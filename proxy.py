#!/usr/bin/env python3
"""
LLM Retry Guard — 零依赖 OpenAI 兼容代理，自动处理 429 和多端点故障转移。

用法:
    python proxy.py                      # 读 config.json，监听 127.0.0.1:8000
    python proxy.py --port 8123          # 指定端口
    python proxy.py --config my.json     # 指定配置

只实现两个端点：
    POST /v1/chat/completions
    GET  /v1/models
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_CONFIG = {
    "endpoints": [],
    "retry": {"max_attempts": 4, "base_delay": 1.0, "max_delay": 30.0, "backoff_factor": 2.0},
    "rate_limit": {"qpm": 0, "burst": 1},
    "cooldown_seconds": 60,
}


class Guard:
    def __init__(self, cfg):
        self.endpoints = cfg.get("endpoints", [])
        self.retry = cfg.get("retry", DEFAULT_CONFIG["retry"])
        self.rate = cfg.get("rate_limit", DEFAULT_CONFIG["rate_limit"])
        self.cooldown = cfg.get("cooldown_seconds", 60)
        self.cool_until = {}                 # endpoint name -> timestamp
        self.calls = deque()                 # 最近一分钟的请求时间戳
        self.lock = threading.Lock()
        self.stats = {"total": 0, "retries": 0, "failover": 0, "failed": 0}

    def _rate_wait(self):
        """主动限速，返回需要等待的秒数。"""
        qpm = self.rate.get("qpm", 0)
        if not qpm:
            return 0.0
        burst = self.rate.get("burst", 1)
        now = time.time()
        with self.lock:
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) < burst:
                self.calls.append(now)
                return 0.0
            wait = 60 - (now - self.calls[0]) + 0.05
        return max(0.0, wait)

    def _available(self, endpoint):
        until = self.cool_until.get(endpoint["name"], 0)
        return time.time() >= until

    def _mark_cooldown(self, endpoint, reason):
        self.cool_until[endpoint["name"]] = time.time() + self.cooldown
        print(f"[guard] {endpoint['name']} 冷却 {self.cooldown}s: {reason}", file=sys.stderr)

    def _call(self, endpoint, payload, timeout=60):
        url = endpoint["base_url"].rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + endpoint.get("api_key", ""),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def chat(self, payload):
        """带重试和故障转移的 chat 调用。返回 (status, body)。"""
        self.stats["total"] += 1
        wait = self._rate_wait()
        if wait:
            time.sleep(wait)

        model = payload.get("model", "")
        candidates = [e for e in self.endpoints
                      if self._available(e) and (not e.get("models") or model in e["models"])]
        if not candidates:
            # 全部冷却中：找第一个强制重试
            candidates = [e for e in self.endpoints if not e.get("models") or model in e["models"]]
        if not candidates:
            return 503, {"error": {"message": "没有可用端点", "type": "no_endpoint"}}

        max_attempts = int(self.retry.get("max_attempts", 4))
        base = float(self.retry.get("base_delay", 1.0))
        factor = float(self.retry.get("backoff_factor", 2.0))
        ceiling = float(self.retry.get("max_delay", 30.0))

        last_status, last_body = 503, {"error": {"message": "未知错误"}}
        for ep in candidates:
            for attempt in range(max_attempts):
                try:
                    status, body = self._call(ep, payload)
                    return status, body
                except urllib.error.HTTPError as e:
                    last_status = e.code
                    try:
                        last_body = json.loads(e.read().decode("utf-8"))
                    except Exception:
                        last_body = {"error": {"message": str(e)}}
                    # 4xx 里只有 408/409/429 值得重试；其它是请求本身的问题
                    if e.code not in (408, 409, 429, 500, 502, 503, 504):
                        return e.code, last_body
                    if e.code == 429:
                        self.stats["retries"] += 1
                        self._mark_cooldown(ep, "429 rate limited")
                        break  # 换下一个端点，不在同一个上死磕
                    delay = min(ceiling, base * (factor ** attempt))
                    print(f"[guard] {ep['name']} HTTP {e.code}，{delay:.1f}s 后重试", file=sys.stderr)
                    time.sleep(delay)
                    self.stats["retries"] += 1
                except Exception as e:
                    last_status = 0
                    last_body = {"error": {"message": f"{type(e).__name__}: {e}"}}
                    self._mark_cooldown(ep, str(e))
                    break  # 网络错误，换端点
            # 这个端点放弃了，试下一个
            if candidates.index(ep) < len(candidates) - 1:
                self.stats["failover"] += 1
                print(f"[guard] 切换到下一个端点", file=sys.stderr)
        self.stats["failed"] += 1
        return last_status, last_body

    def models(self):
        seen, out = set(), []
        for e in self.endpoints:
            for m in e.get("models", []):
                if m not in seen:
                    seen.add(m)
                    out.append({"id": m, "object": "model", "owned_by": e["name"]})
        return out


class Handler(BaseHTTPRequestHandler):
    guard = None

    def log_message(self, fmt, *args):
        pass  # 安静点，避免刷屏

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list", "data": self.guard.models()})
        elif self.path.rstrip("/") in ("/health", "/stats"):
            self._json(200, self.guard.stats)
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._json(400, {"error": {"message": f"bad json: {e}"}})
            return
        status, body = self.guard.chat(payload)
        self._json(status, body)


def main():
    ap = argparse.ArgumentParser(description="LLM Retry Guard")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.is_absolute():
        path = Path(__file__).parent / args.config
    if not path.exists():
        print(f"找不到配置 {path}，请先 cp config.example.json config.json", file=sys.stderr)
        sys.exit(2)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not cfg.get("endpoints"):
        print("配置里没有 endpoints", file=sys.stderr)
        sys.exit(2)

    Handler.guard = Guard(cfg)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[guard] {len(cfg['endpoints'])} 个端点，监听 http://{args.host}:{args.port}/v1")
    print(f"[guard] 统计: http://{args.host}:{args.port}/stats")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[guard] 退出，统计:", Handler.guard.stats)


if __name__ == "__main__":
    main()
