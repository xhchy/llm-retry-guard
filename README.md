# LLM Retry Guard / 免费 LLM 车道限流守护

<p align="center"><b>一个包装层，让免费 LLM API 的 429 不再打断你的任务。</b></p>

## 痛点

免费 LLM API（Glm-4.5-Flash、各种公益中转、OpenRouter 免费档）都有一个共同问题：

- 429 来得毫无征兆
- 大多数客户端收到 429 直接失败，不会重试
- 免费额度是宝贵的，盲目重试又会加剧限流
- 多车道切换要手写一堆胶水代码

**结果**：任务跑到一半被 429 打断，前面所有工作白费。

## 解决

`llm-retry-guard` 是一个**零依赖的 OpenAI 兼容代理**：

- ✅ 429 / 5xx 自动指数退避重试（可配置）
- ✅ 多端点故障转移：主车道挂了自动切备用
- ✅ 尊重 `Retry-After` 响应头
- ✅ 每个端点独立冷却，不会反复撞墙
- ✅ 全局 QPM 限速，主动避免触发限流
- ✅ 零依赖，Python 标准库即可运行
- ✅ 完整的调用日志，看得到每次重试的原因

## 快速开始

```bash
# 1. 复制配置模板
cp config.example.json config.json

# 2. 编辑 config.json，填入你的端点
# 3. 启动
python proxy.py

# 代理跑在 http://127.0.0.1:8000/v1
# 你的代码只需把 base_url 改过来，其余不变
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
resp = client.chat.completions.create(
    model="copilot",
    messages=[{"role": "user", "content": "Hello"}],
)
```

## 配置

```json
{
  "endpoints": [
    {"name": "primary", "base_url": "https://api.example.com/v1", "api_key": "xxx", "models": ["glm-4.5-flash"]},
    {"name": "backup",  "base_url": "http://127.0.0.1:18899/v1", "api_key": "local", "models": ["mimo-v2.6-flash-free"]}
  ],
  "retry": {
    "max_attempts": 4,
    "base_delay": 1.0,
    "max_delay": 30.0,
    "backoff_factor": 2.0
  },
  "rate_limit": {
    "qpm": 12,
    "burst": 4
  },
  "cooldown_seconds": 60
}
```

## 退避策略

| 尝试 | 等待 |
|---|---|
| 1 | 1s |
| 2 | 2s |
| 3 | 4s |
| 4 | 8s（上限 30s） |

如果响应带 `Retry-After` 头，优先服从服务端指定值。

## 为什么不直接用十几个现成的网关？

那些项目要么依赖一堆包、要么面向生产环境、要么需要数据库。
这个只解决一件事：**让本地开发/定时任务不被 429 打断**。
单文件、零依赖、改个 base_url 就能用。

## 许可

MIT
