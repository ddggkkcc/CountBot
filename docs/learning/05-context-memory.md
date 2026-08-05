# 上下文与记忆管理深度解析：三层信息架构

> 源码：[backend/modules/session/context_service.py](backend/modules/session/context_service.py) (~660行) + [backend/modules/agent/memory.py](backend/modules/agent/memory.py) (~240行) + [backend/modules/agent/context.py](backend/modules/agent/context.py) (~770行)
> 难度：⭐⭐⭐ 工程深度高，需要理解缓存策略和异步并发
> 预计学习时间：2 天

---

## 📋 前置知识

- LLM 的 Context Window（上下文窗口）概念
- 缓存策略：增量更新 vs 全量重建
- Python `asyncio.Task`、`AsyncSession`
- 读完 [01-agent-loop.md](01-agent-loop.md)（了解 AgentLoop 怎么使用上下文）

---

## 🎯 核心概念

### 这个模块解决什么问题？

LLM 的上下文窗口有限（比如 128K tokens），但对话历史可能很长。你不能把 3 小时前的聊天记录全塞进去——太贵、太慢、而且 LLM 容易在长上下文中「迷失」。

CountBot 用三层架构解决：

```
第 1 层：短期缓存（Short Context Summary）
    │ 作用：把超出窗口的历史消息压缩成一个摘要，作为 system 消息注入
    │ 特点：增量维护 —— 只处理新增的消息，不重算已有部分
    │ 触发时机：历史消息数 > max_history_messages 时
    │ 存储位置：Session 表的 short_context_summary 字段
    ▼
第 2 层：溢出沉淀（Overflow to Memory）
    │ 作用：把超出窗口的历史总结后写入长期记忆文件
    │ 特点：跳过无意义的闲聊，只保留有价值信息
    │ 触发时机：每次请求后自动检查
    │ 存储位置：workspace/memory/MEMORY.md
    ▼
第 3 层：整会话归档（Auto Summarize to Memory）
    │ 作用：当对话足够长时，整场会话的精华自动归档到长期记忆
    │ 特点：≥30 条消息 或 ≥15000 字符才触发
    │ 存储位置：workspace/memory/MEMORY.md
```

### 两类「记忆」

```
会话记忆（Session Memory）              长期记忆（Long-term Memory）
├── 存在 SQLite 数据库                   ├── 存在文件系统（MEMORY.md）
├── 和会话绑定，切换会话就换              ├── 跨会话，Agent 始终能看到
├── 自动管理（消息持久化）                ├── Agent 主动写入（memory 工具）
└── 三层压缩策略（见上）                 └── 行式格式：日期|来源|内容
```

---

## 🔍 关键代码路径

### 路径 1：请求时的上下文构建 [context_service.py:120-186]

```python
async def build_model_context(self, session_id, max_history_messages, ...):
    session = await self.get_session(session_id)

    # 尝试命中短期摘要缓存
    if (
        enable_short_context_summary
        and session.short_context_summary     # 有缓存
        and session.short_context_summary_msg_id  # 知道覆盖到哪条消息
    ):
        # 命中！用摘要替代历史消息
        tail_messages = await self._load_messages(
            session_id,
            after_message_id=session.short_context_summary_msg_id
        )
        history = [summary_as_system_message] + [tail messages]
        return ConversationModelContext(
            history=history,
            short_summary_used=True,  # 标记：本次用了缓存
        )

    # 未命中：直接加载最近 N 条消息
    recent_messages = await self._load_messages(session_id, limit=max_history_messages)
    return ConversationModelContext(history=recent_messages, ...)
```

**为什么是增量？** — `after_message_id` 是关键。不是每次都把所有历史消息送给 LLM 重新总结。第一次总结 50 条 → 标记 `covered_until_msg_id=50`。下次只需处理消息 #51-#60，和已有摘要做递归合并。

### 路径 2：短期摘要缓存刷新 [context_service.py:188-289]

```python
async def refresh_short_summary_cache(self, ...):
    messages = await self._load_messages(session_id)

    # 消息不够多 → 不需要摘要，清空缓存
    if len(messages) <= max_history_messages:
        return await self._clear_short_summary_cache(...)

    # 分离：prefix（需要压缩的旧消息）+ tail（保留的最近消息）
    prefix_messages = messages[:-max_history_messages]

    # 检查是否可以增量合并
    if session.short_context_summary and ...:
        # 增量模式：只压缩新增的消息，和已有摘要合并
        previous_summary = session.short_context_summary
        delta_messages = [m for m in prefix_messages if m.id > previous_covered_until]
        summary = await self._summarize_short_context(
            ..., previous_summary=previous_summary, messages=delta_messages
        )
    else:
        # 首次生成：压缩所有 prefix 消息
        summary = await self._summarize_short_context(
            ..., previous_summary="", messages=prefix_messages
        )

    # 保存缓存
    session.short_context_summary = summary
    session.short_context_summary_msg_id = prefix_messages[-1].id
    await self.db.commit()
```

**核心优化**：不是每次都调用 LLM 重新总结。大多数请求只新增了几条消息，增量合并只需要 LLM 处理这几条，cost 和 latency 几乎为零。

### 路径 3：溢出历史 → 长期记忆 [context_service.py:291-368]

```python
async def summarize_overflow_to_memory(self, ...):
    messages = await self._load_messages(session_id)
    overflow_messages = messages[:-max_history_messages]  # 超出窗口的

    # 只处理还没总结过的部分
    pending = [m for m in overflow_messages if m.id > session.last_summarized_msg_id]

    # 过滤：只要 user 和 assistant 的消息，跳过 tool 和 system
    to_summarize = [m for m in pending if m.role in {"user", "assistant"}]

    # 消息太少不值得总结
    if len(to_summarize) < 3:
        return False

    # 调用 LLM 总结 → 写入 MEMORY.md
    summary = await provider.chat_stream(prompt=OVERFLOW_SUMMARY_PROMPT)
    if "无需记录" not in summary:  # LLM 的判断：这些内容不值得长期记住
        memory_store.append_entry(source="auto-overflow", content=summary)
```

**关键细节**：LLM 自己判断「值不值得记住」——如果溢出内容只是天气查询、测试消息等，LLM 会返回「无需记录」，系统就不写入长期记忆。

### 路径 4：后台维护任务的合并去重 [context_service.py 末尾]

```python
def schedule_context_maintenance(session_id, ...):
    # 如果已有同 session 的维护任务在运行 → 不创建新的
    # 把新请求存为 pending，当前任务完成后自动处理
    existing_task = _CONTEXT_MAINTENANCE_TASKS.get(session_id)
    if existing_task and not existing_task.done():
        _PENDING_CONTEXT_MAINTENANCE[session_id] = request
        return existing_task  # 返回已有任务，调用方等待它完成

    # 创建新任务
    task = asyncio.create_task(_runner(request))
    _CONTEXT_MAINTENANCE_TASKS[session_id] = task
```

**为什么需要合并**：用户连续快速发消息时，每条消息都触发维护 → 5 条消息触发了 5 个维护任务。合并后只需要跑一次（处理所有累积的新消息）。

### 路径 5：System Prompt 构建 [context.py:93-508]

`build_system_prompt()` 是发给 LLM 的第一条消息（role=system），包含：
1. **核心身份**：AI 的名字、角色、时间、运行环境
2. **性格设定**：从数据库/配置加载
3. **自动加载的技能**：标记为 `auto_load: true` 的技能全文
4. **可用技能摘要**：按需加载的技能列表（不加载全文，减少 token）
5. **激活的 Agent 团队**：从数据库读取，注入到 prompt 中提示 LLM 可以用 @team_name 调用
6. **工具使用规则**：文件操作规则、安全准则等
7. **外部编码代理引导**：根据配置决定是否告诉 LLM「你可以用 Claude Code」

---

## 💡 可深挖的逻辑

### 深度点 1：短期摘要的「递归合并」Prompt

`RECURSIVE_SHORT_CONTEXT_SUMMARY_PROMPT` 的实现（在 `prompts.py` 中）。这个 prompt 需要 LLM 做到：
- 保留已有摘要中的关键信息
- 融入新增消息中的新信息
- 控制在字符限制内

**延伸思考**：如果 LLM 在合并时丢失了已有摘要中的重要信息，怎么办？这是一个经典的「摘要漂移（summary drift）」问题。

### 深度点 2：MemoryStore 的行式 vs 文件式设计

当前 MemoryStore 使用简单的行式文本格式：`日期|来源|内容`。优点是极简、人类可读，缺点是：
- 不支持按类型过滤（用户偏好 vs 项目知识 vs 反馈全混在一起）
- 不支持增量更新（只能删行重写）
- 不支持交叉引用

[architecture-optimization.md](../architecture-optimization.md) 的第 6 条建议就是改进这个设计。

### 深度点 3：消息加载的查询策略

`_load_messages()` 支持两种模式：
- `limit=N`：加载最近 N 条（DESC 排序 + LIMIT + reverse）
- `after_message_id=M`：加载 ID > M 的所有消息（ASC 排序 + WHERE）

两种模式服务于不同的上下文场景：前者用于首次加载，后者用于增量补充。

---

## 🎤 面试话术

### 2 分钟版

> CountBot 的上下文管理是三层架构。第一层是短期摘要缓存——当对话历史超出窗口限制时，把旧消息压缩成摘要而不是直接截断。关键优化是增量维护——不是每次请求都重算，而是只处理新增的消息然后和已有摘要递归合并，大部分请求的 LLM 调用开销接近零。
>
> 第二层是溢出沉淀——超出窗口的历史中如果包含有价值的信息（不是闲聊），自动总结后写入长期记忆文件。
>
> 第三层是整会话归档——当对话超过 30 条消息或 15000 字符时，把整场对话中值得保留的部分自动归档。
>
> 还有一个细节是后台维护任务的合并去重——用户连续快速发消息时，多个维护请求会合并成一次执行，避免重复调用 LLM。

---

## ✏️ 练习建议

### 练习 1：观察上下文构建（30 分钟）
在 `build_model_context()` 入口打日志，发起一次多轮对话，观察：
- 每次请求加载了多少条历史消息
- 什么时候触发了短期摘要
- 摘要命中时，历史消息从多少条减少到多少条

### 练习 2：手动触发记忆归档（30 分钟）
和 Agent 进行 35 轮以上的对话（可以快速发「你好」「1+1=?」等），观察：
- 什么时候触发了溢出总结
- MEMORY.md 中写入了什么内容

### 练习 3：阅读 System Prompt（15 分钟）
在 `build_system_prompt()` 最后加一行 `logger.info(system_prompt)`，发起一次对话，完整阅读发给 LLM 的 system prompt，理解它包含了哪些信息、每条信息的目的是什么。
