# 前置知识速览：从后端开发到 AI Agent

> 面向有 2 年后端经验的开发者，补充进入 AI Agent 领域所需的基础概念。

---

## 你需要知道什么

### 已经有了的（后端基础）

| 概念 | 说明 |
|------|------|
| HTTP/REST API | FastAPI 的路由、请求/响应模型 |
| 异步编程 | Python `async/await`、`asyncio` |
| 数据库 | SQLAlchemy ORM、SQLite |
| WebSocket | 全双工通信 |
| 设计模式 | 策略模式、工厂模式、IoC、单例 |
| JSON Schema | 参数验证和结构化数据描述 |

### 需要补充的（AI/LLM 基础）

---

## 1. LLM 是怎么工作的

### 1.1 Token（令牌）

LLM 不是按「字」来理解文本的，而是按 **token**。一个 token 大约等于：
- 英文：~0.75 个单词
- 中文：~0.5-1 个汉字

**为什么重要**：LLM 按 token 计费，且有上下文窗口（context window）限制。CountBot 的上下文管理系统就是为了在有限的 token 预算内塞入最多的有用信息。

```python
# 一个直观的例子
"Hello world" → 2 tokens
"你好世界" → 3-4 tokens
```

### 1.2 System Prompt vs User Message vs Assistant Message

LLM 的输入是一组「消息」，每一条有角色：

```
[
  {"role": "system", "content": "你是一个有用的助手"},     # 系统提示词：定义角色和规则
  {"role": "user", "content": "今天天气怎么样？"},          # 用户消息
  {"role": "assistant", "content": "请问你在哪个城市？"},   # AI 回复
  {"role": "user", "content": "北京"},                      # 用户回复
]
```

- **system**：定义 AI 的角色、能力边界、行为规则（在 CountBot 中由 `ContextBuilder.build_system_prompt()` 生成）
- **user**：用户输入
- **assistant**：AI 的历史输出
- **tool**：工具调用的结果（见下文）

**在 CountBot 中的对应**：`ContextBuilder.build_messages()` [context.py:512-566](backend/modules/agent/context.py#L512-L566)

### 1.3 Function Calling / Tool Use

这是 Agent 区别于普通 Chatbot 的核心能力。LLM **不只是生成文本**——它能决定「我现在需要调用一个函数」。

**流程**：
```
1. 你告诉 LLM：「你有这些工具可用：read_file, exec, web_fetch...」
   ↓
2. LLM 推理后决定：「我需要先读取错误日志」
   ↓
3. LLM 返回的不是文本，而是一个 tool_call：
   {"name": "read_file", "arguments": {"path": "data/logs/app.log"}}
   ↓
4. 你的程序执行 read_file("data/logs/app.log")
   ↓
5. 把执行结果追加回对话：「read_file 的结果：Error: connection timeout...」
   ↓
6. LLM 看到结果后继续推理：「是连接超时，我建议检查网络配置」
```

**在 CountBot 中的对应**：`AgentLoop.process_message()` 中的 tool_calls_buffer 处理 [loop.py:331-606](backend/modules/agent/loop.py#L331-L606)

### 1.4 Temperature（温度）

控制 LLM 输出的「随机性」，范围 0-2：
- **0**：确定性最强，同样的输入得到同样的输出（适合代码生成、事实查询）
- **0.7-1**：有一定创造性（适合写作、头脑风暴）
- **>1**：非常随机，可能胡言乱语

CountBot 默认 `temperature=0.0`，偏向确定性和可靠性。

### 1.5 上下文窗口（Context Window）

LLM 一次能「看到」的最大 token 数。超过这个限制：
- 最旧的消息会被截断
- 或者你需要做总结/压缩

CountBot 的三层上下文管理就是为了解决「窗口不够用但信息不能丢」的矛盾。

---

## 2. AI Agent 核心概念

### 2.1 什么是 Agent

> **Agent = LLM + 工具 + 循环**

传统程序：
```python
if user_input == "查天气":
    result = weather_api("北京")
    return f"北京今天{result}"

# 问题：只能处理预定义的情况
```

Agent：
```python
# 你只需要告诉 LLM 有什么工具，它自己决定用什么
tools = [weather_api, file_reader, shell_executor, web_searcher]
# LLM 推理：「用户想知道天气 → 我需要调 weather_api → 参数是"北京"」
# 如果 weather_api 失败了，LLM 可能会自己尝试 web_searcher 作为替代
```

### 2.2 ReAct 模式（Reasoning + Acting）

Agent 核心工作模式，来自 2022 年 Yao et al. 的论文：

```
Loop:
  1. Thought（思考）: 「用户想知道天气，我需要调 weather_api」
  2. Action（行动）: 执行 weather_api(city="北京")
  3. Observation（观察）: 拿到结果 "25°C，晴"
  4. Thought: 「信息足够了，我可以回答用户了」
  5. 输出最终回复
```

**在 CountBot 中的对应**：`AgentLoop.process_message()` 的整个 while 循环

### 2.3 Multi-Agent（多智能体协作）

多个 Agent 协同工作：
- **Pipeline（流水线）**：一个接一个，像工厂流水线
- **Graph（依赖图）**：有依赖关系的并行执行
- **Council（评审会）**：多视角独立分析后交叉讨论

**在 CountBot 中的对应**：`WorkflowEngine` [workflow.py](backend/modules/agent/workflow.py)

### 2.4 MCP（Model Context Protocol）

Anthropic 提出的 Agent-to-Tool 通信标准协议。让 Agent 以标准化方式连接外部工具服务。

- **stdio 传输**：启动一个子进程，通过标准输入/输出通信
- **SSE 传输**：通过 HTTP Server-Sent Events
- **Streamable HTTP**：通过 HTTP 流式传输

**在 CountBot 中的对应**：[client.py](backend/modules/mcp/client.py)

---

## 3. 学习路径推荐

### 如果你只有一周

| 天 | 内容 | 目标 |
|----|------|------|
| 1 | 本文 + 动手跑起来项目 | 建立全局认知 |
| 2 | [Agent Loop 深度解析](01-agent-loop.md) | 理解核心循环 |
| 3 | [Provider 层深度解析](02-provider-layer.md) | 理解 LLM 抽象 |
| 4 | [Tool 系统深度解析](03-tool-system.md) | 理解工具机制 |
| 5 | [Workflow 引擎深度解析](04-workflow-engine.md) | 理解多 Agent |
| 6-7 | [上下文与记忆](05-context-memory.md) + [MCP](06-mcp-client.md) | 补齐周边 |

### 如果你有两周

再加上：
- [Channel 系统](07-channel-system.md)
- 动手修改一个工具，观察 Agent 行为变化
- 创建一个自定义 Agent Team，尝试 Pipeline/Graph/Council 三种模式
- 阅读 [架构优化文档](../architecture-optimization.md)，理解设计权衡

---

## 4. 关键文件速查（阅读顺序）

| 优先级 | 文件 | 为什么先读 | 行数 |
|--------|------|-----------|------|
| ⭐⭐⭐ | `backend/modules/agent/loop.py` | 项目心脏，ReAct 循环 | ~730 |
| ⭐⭐⭐ | `backend/modules/providers/base.py` | LLM 抽象基类，理解统一接口 | ~50 |
| ⭐⭐⭐ | `backend/modules/tools/base.py` | 工具抽象基类 | ~50 |
| ⭐⭐ | `backend/modules/agent/workflow.py` | 多 Agent 编排 | ~600 |
| ⭐⭐ | `backend/modules/agent/context.py` | 上下文构建 | ~770 |
| ⭐⭐ | `backend/modules/providers/openai_provider.py` | LLM 调用具体实现 | ~1200 |
| ⭐ | `backend/modules/agent/subagent.py` | 子 Agent 管理 | ~890 |
| ⭐ | `backend/modules/session/context_service.py` | 上下文维护 | ~660 |
| ⭐ | `backend/modules/mcp/client.py` | MCP 协议 | ~880 |

---

## 5. 术语中英对照

| 中文 | 英文 | 说明 |
|------|------|------|
| 大语言模型 | LLM (Large Language Model) | GPT-4, Claude, DeepSeek 等 |
| 提示词/提示 | Prompt | 发给 LLM 的文本指令 |
| 系统提示词 | System Prompt | 定义 AI 角色和行为边界的提示词 |
| 工具调用 | Tool Call / Function Calling | LLM 决定调用某个函数 |
| 推理 | Reasoning | LLM 的「思考」过程 |
| 流式输出 | Streaming | 逐 token 返回，不等完整结果 |
| 上下文窗口 | Context Window | LLM 一次能处理的最大 token 数 |
| 令牌 | Token | LLM 处理文本的最小单位 |
| 幻觉 | Hallucination | LLM 编造不存在的「事实」|
| 嵌入 | Embedding | 把文本转成向量，用于语义搜索 |
| RAG | Retrieval-Augmented Generation | 检索增强生成：先搜资料再回答 |
