# CountBot 端到端架构与流程说明

> 把分散在 `/core/*` 各页的组件，拼成"消息从哪进 → 怎么走 → 从哪出"的完整链路。
> 整理日期：2026-08-05 ｜ 基于已读文档（overview / agent-loop / tools / memory / channels / agent-teams / external-coding-tools / subagent）

---

## 一、一句话总览

```
消息入口(Web/IM/定时/CLI)
   → 渠道与路由层(判断账号 / 会话作用域 / routing_mode)
   → 路由分叉:
        ai        → AgentLoop(核心循环)
        direct    → 外部编码工具(Claude Code 等)
        /team @团队 → 智能体团队(workflow_run)
   → AgentLoop 内调用: 工具系统 / 子代理(spawn) / 团队(workflow_run) / 记忆
   → 响应出口(WebSocket / 原渠道 / 进程返回)
```

贯穿全局的横切能力：**记忆系统（长期沉淀）**、**审计日志（每次工具调用）**、**安全隔离（工作空间/危险命令）**、**配置优先级（会话>团队>全局>Provider）**。

---

## 二、分层架构

| 层 | 职责 | 关键组件 / 文件 |
|----|------|----------------|
| **接入层** | 接收外部消息 | Web(WebSocket)、IM 渠道×8、Cron、CLI |
| **路由层** | 账号/会话归属 + 模式选择 | ChannelManager、`routing_mode`(ai/direct)、`/team`/`@团队` |
| **执行层** | 推理 + 工具调用循环 | `AgentLoop`(`backend/modules/agent/loop.py`)、`ContextBuilder`(`context.py`) |
| **能力层** | 可被调用的原子/复合能力 | `ToolRegistry`、子代理(`subagent.py`)、团队(`agent-teams`)、外部编码工具 |
| **持久层** | 状态与记忆落地 | `MemoryStore`(`workspace/memory/MEMORY.md`)、`data/audit_logs/`、Workspace 文件 |
| **横切层** | 安全 / 审计 / 配置 | `SecurityConfig`、审计日志、`模型配置优先级` |

---

## 三、端到端主流程（逐步 + 文件标注）

### Step 1 — 消息进入接入层
- **Web Chat**：通过 WebSocket 进入，`process_message()` 返回 `AsyncIterator[str]` 流式响应。
- **IM 渠道**：telegram / qq / wechat / dingtalk / feishu / weibo / wecom / xiaozhi，消息由对应适配器推入。
- **定时任务**：以 `channel="cron"` 身份调用 `process_direct()`。
- **CLI**：直接 `process_direct()` 拿完整字符串。

### Step 2 — 渠道与路由层
文件：渠道配置 + `ChannelManager`
- 判断**会话作用域**：来自哪个渠道 / 哪个账号 / 群聊还是私聊 / 是否主账号共享群聊上下文。
- 读取账号级默认值：`enabled`、`routing_mode`(ai/direct)、`external_coding_profile`。
- 聊天内可用 `/route`、`/coder` 临时覆盖（不回写后台）。

### Step 3 — 路由分叉（三选一）
- **routing_mode = ai** → 进入主 Agent（`AgentLoop`）。
- **routing_mode = direct** → 消息**直接转发**给绑定的外部编码工具（绕过主 Agent，适合持续编码）。
- **`/team 团队名` 或 `@团队名`** → 触发 `workflow_run`（团队工作流）。

### Step 4 — AgentLoop 核心循环
文件：`backend/modules/agent/loop.py`
```
process_message():
  1. ContextBuilder.build_messages()  → system prompt + 历史 + 当前消息
                                       （注入 记忆 + 已激活技能 + persona + 安全准则）
  2. provider.chat_stream()           → 逐 chunk 流式输出
  3. 有 tool_calls?
       是 → ToolRegistry.execute()（参数校验 → 执行 → 审计日志，失败重试×3，API Key 轮换）
            → 结果追加回消息列表 → 回到 2
       否 → 返回响应
终止: 无 tool_calls / 达 max_iterations(25) / cancel_token / LLM 报错
```

### Step 5 — 能力层调用（AgentLoop 内部可触发的分支）
| 调用 | 触发工具 | 去哪 | 关键特征 |
|------|----------|------|----------|
| 原子能力 | `read_file`/`exec`/`web_fetch`/`screenshot`/`file_search`/`memory`/`send_media` | `ToolRegistry`（`tools/registry.py`） | 每次调用记审计日志 |
| 后台子任务 | `spawn` | `SubagentManager` | 独立 AgentLoop、最多15迭代、Shell 300s、**不可再 spawn** |
| 多角色协作 | `workflow_run` | 智能体团队 | pipeline/graph/council 三模式 |
| 外部编码 | 由 `direct` 路由或显式触发 | Claude Code / Codex / OpenCode | `session_mode`: stateless/history/native |

### Step 6 — 记忆系统（贯穿）
文件：`backend/modules/agent/memory.py`
- `memory` 工具：`write` / `search` / `read`，落盘 `workspace/memory/MEMORY.md`（`日期|来源|内容`）。
- 会话超窗时自动 `auto-overflow` 总结写入记忆，保持上下文轻量。

### Step 7 — 响应出口
- **Web**：WebSocket 流式推送 `{"type":"stream","content":chunk}`。
- **IM**：经 `ChannelManager` 的 `send_message` / `send_media` 回原渠道账号。
- **Cron / CLI**：`process_direct()` 返回完整字符串。

---

## 四、三条分支路径详解

### 路径 A：外部编码工具（direct 模式）
```
IM 消息 → 账号 routing_mode=direct + external_coding_profile
  → 直接启动 CLI（claude / codex / opencode）
  → session_mode 决定带多少历史(stateless/history/native)
  → 结果回原渠道
```
注意：若 `direct` 但未绑定 `external_coding_profile`，渠道配置**保存时直接拒绝**。

### 路径 B：智能体团队（workflow_run）
```
workflow_run(team_name | inline agents)
  → 加载 mode + agents + cross_review + 团队模型
  → pipeline: 串行，后者可见前者结果
  → graph:   按 depends_on 并行，condition 控制条件执行
  → council: 并行观点 + (cross_review 时) 第二轮交叉评议
  → 结果回调用方
```

### 路径 C：子代理（spawn）
```
AgentLoop 调 spawn(task)
  → SubagentManager.create_task() → asyncio 后台异步
  → 独立 AgentLoop(最多15迭代, Shell 300s, 无 spawn/无 send_media/无独立 memory)
  → 完成 → WebSocket 通知前端(进度 0→100%)
```

---

## 五、横切关注点

- **记忆**：跨所有渠道统一沉淀，按来源(web-chat/telegram/cron/auto-overflow)标记，可搜索。
- **审计**：每次工具调用写入 `data/audit_logs/`（call_id/tool/args/status/duration/result），由 `SecurityConfig.audit_log_enabled` 开关。
- **安全**：`WorkspaceValidator` 限制路径在工作空间内；`exec` 有 `DANGEROUS_PATTERNS` 黑名单 + 可选白名单 + 超时(主30s/子300s) + 输出截断。
- **配置优先级**：`会话自定义 > 团队专属 > 全局 > Provider 默认`（模型与 persona 均适用）。

---

## 六、本图未展开的部分（诚实标注）

以下在文档中有独立页面，但本次未深读，故未画入主链路：
- **部署（`/advanced/deployment`）**：进程模型、多实例、反向代理等运行时骨架。
- **安全机制（`/advanced/security`）**：鉴权、密钥管理、权限模型的完整细节。
- **API 参考（`/api-reference`）**：REST/WebSocket 全量接口清单。

---

## 七、与"状态控制 / HITL"的衔接点（呼应前几轮讨论）

如果要在 CountBot 加"agent 主动暂停等人确认/澄清"（HITL），**最自然的落点就是 Step 4 的 AgentLoop 循环**：

- 在循环里新增 run-level 状态 `AWAITING_USER_INPUT`；
- 新增工具 `ask_user(question, options?)`：调用即置状态、落盘待问记录、停止推理、把问题推到当前渠道；
- 用户回复后，把回答作为 tool result 回灌，循环继续；
- 这样澄清、规划确认、优先级征询、危险操作审批，全共用同一套"暂停-等待-恢复"原语。

换句话说：**AgentLoop 是系统的心脏，也是接入状态控制唯一的合理中枢。**
