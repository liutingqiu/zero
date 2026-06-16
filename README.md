# ⚡ 零 (Zero) — AI OS Kernel

> 一句话落地：输入任务，AI 团队自动协作完成。

[![Python](https://img.shields.io/badge/Python-3.11+-blue)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-blue)](https://hub.docker.com)

---

## 是什么

零是一个**多 Agent 协作平台**——你说一句话，系统自动调度 Planner（规划者）、Executor（执行者）、Critic（审查者）、Synthesizer（整合者）四个 AI Agent 协作完成任务，**实时展示思考过程**。

```
你说："帮我做一个响应式网站"
  ↓
📋 Planner   → 拆解为 3 个步骤
🔧 Executor  → 逐步执行（写 HTML → 写 CSS → 写 JS）
🔍 Critic    → 审查每步结果，发现问题自动修正
📝 Synthesizer → 整合输出完整网站代码
  ↓
✅ 完成
```

---

## 快速开始

### Docker（推荐）

```bash
# 1. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 AGNES_API_KEY 和 LLM_API_KEY

# 2. 启动
docker compose up -d

# 3. 打开
open http://localhost:5052
```

### 本地运行

```bash
# 安装依赖
pip install aiohttp httpx

# 设置环境变量
export AGNES_API_KEY=your_key    # Agnes 免费 API
export LLM_API_KEY=your_key      # DeepSeek API

# 启动
python zero_server.py
```

---

## API

| 端点 | 说明 |
|------|------|
| `GET /health` | 健康检查 |
| `POST /api/chat` | 单 Agent 对话 |
| `GET /api/chat/stream` | SSE 流式聊天 |
| `POST /api/collab` | 多 Agent 协作 |
| `GET /api/collab/stream` | SSE 流式协作（实时黑板） |

---

## 架构

```
zero/
├── app/api/           # HTTP 层 (aiohttp)
├── app/services/      # LLM 服务 (同步+异步)
├── behavior/          # 行为控制 (Gate+Control+Canon+Calibrate+Evaluate+Ground)
├── multi_agent/       # 多Agent (Events+Consensus+Contracts+Blackboard+Agents+Orchestrator)
├── semantic_gateway.py # 语义网关 (L1阻断+L2标准化+L3约束)
├── infrastructure/    # 沙箱 (Docker/Windows/NoOp)
└── security/          # 安全 (Guard+Sandbox)
```

---

## 接入模型

支持任意 **OpenAI 兼容接口**——有 API Key 就能接。配置两行即可：

```bash
AGNES_API_KEY=sk-xxx    # 免费文本+生图 (推荐)
LLM_API_KEY=sk-xxx      # DeepSeek / GPT / 任何兼容接口
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
```

也支持本地模型 (Ollama)，在 `zero_config.json` 里加一行就行。

---

## License

MIT © 柳橙
