# CountBot 架构优化路线图

## 概述

本文档基于 CountBot 与 Claude Code Harness 的架构对比分析，梳理当前可借鉴的优化方向。每条建议包含**当前痛点**（为什么）、**目标方案**（怎么做）和**使用场景**（何时生效）。

优先级标记：
- 🔴 **P0 必须做** — 安全基础或架构关键缺口，不做则后续优化无处落脚
- 🟡 **P1 应该做** — 显著提升用户体验和系统质量，投入产出比高
- 🟢 **P2 可以做** — 锦上添花，在 P0/P1 完成后择机实施

---

## 🔴 P0 — 安全与架构基础

---

### 1. Hooks 系统（工具生命周期钩子）

**为什么：**

当前 CountBot 只有 `tool_event_handler` 用于执行中的进度推送（WebSocket → 前端渲染进度条），没有前置审批、没有后置处理。这意味着：
- 所有工具调用一旦触发就立即执行，无法中断
- 无法在工具执行前后注入自定义逻辑（通知、审计增强、结果加工）
- 任何扩展（权限审批、输出扫描、自动备份）都缺乏统一的插入点，只能硬编码到每个工具里

Hooks 是所有后续优化（权限审批、输出安全扫描、执行审计增强等）的**基础设施**。没有它，每个工具都要单独改造，成本线性增长。

**目标方案：**

在 `ToolRegistry.execute()` 的前后加入标准 hook 点：

```
[调用到达] → before_execute hooks → [工具执行] → after_execute hooks → [返回结果]
                    ↓                                      ↓
              可拒绝/修改参数                        可加工结果/触发副作用
```

Hook 接口定义：

```python
class ToolHook(Protocol):
    async def before_execute(
        tool_name: str, params: dict, context: ToolExecutionContext
    ) -> bool | dict:
        """返回 False=拒绝执行, dict=修改后的参数, True=原样放行"""
        ...

    async def after_execute(
        tool_name: str, params: dict, result: str, context: ToolExecutionContext
    ) -> str | None:
        """返回 str=替换原结果, None=不修改"""
        ...
```

Hook 注册方式（按工具名 + 可选频道/会话过滤）：

```python
registry.add_hook(
    tool_name="exec",
    hook=DangerousCommandApprovalHook(),
    priority=10,                    # 数字越小越先执行
    scope="channel:dingtalk"        # 仅飞书渠道生效
)
```

**使用场景：**

| 场景 | Hook 类型 | 行为 |
|------|-----------|------|
| 飞书用户执行 `exec` 危险命令 | `before_execute` | 推送确认卡片到飞书，用户回复「同意」后才执行 |
| 文件写入后自动备份 | `after_execute` | `write_file` 成功后，自动 `cp` 到 `.backup/` 目录 |
| 输出内容安全扫描 | `after_execute` | `exec` 结果中检测到 API key 正则时，自动打码或告警 |
| 审计增强 | `after_execute` | 将完整的工具调用上下文写入外部审计系统 |
| 开发调试 | `before/after` | 打印工具调用参数和耗时，不影响生产环境 |

---

### 2. 工具权限分级

**为什么：**

当前所有工具一旦注册就对所有会话、所有渠道、所有用户开放。唯一的「权限控制」是 shell 的 9 条危险命令黑名单，且 `restrict_to_workspace` 默认值为 `False`——这意味着默认配置下，shell 可以做任何事。

实际部署中，不同渠道的安全性需求截然不同：
- Web 管理面板：管理员使用，可以开放全部工具
- 飞书/钉钉渠道：企业员工使用，应该限制敏感操作
- 公开机器人：不应有任何写操作

**目标方案：**

在 `AppConfig` 中增加工具权限模型，支持三级动作（`allow` / `deny` / `ask`）：

```python
class ToolPermission:
    tool_name: str           # "exec" | "spawn" | "write_file" | ...
    action: str              # "allow" | "deny" | "ask"
    scope_type: str          # "global" | "channel" | "session" | "user"
    scope_value: str         # "*" | "dingtalk" | "session:abc123"
    path_pattern: str | None # 针对文件/命令的路径匹配（glob）
```

注册工具时注入权限检查器：

```python
# setup.py
registry.register(
    ExecTool(...),
    default_permission=ToolPermission(
        tool_name="exec",
        action="ask",          # 默认需要确认
        scope_type="global",
        scope_value="*"
    )
)
```

渠道级覆盖：

```yaml
# config 中的权限配置
tool_permissions:
  - tool: "exec"
    action: "allow"
    scope: "channel:web"       # Web 管理面板直接放行
  - tool: "exec"
    action: "deny"
    scope: "channel:feishu"    # 飞书禁止 shell
  - tool: "write_file"
    action: "ask"
    scope: "channel:*"          # 所有渠道写文件需确认
```

**使用场景：**

| 场景 | 配置 | 效果 |
|------|------|------|
| 企业内部飞书 Bot | `exec: deny, spawn: deny` | 员工只能对话和查资料，不能执行命令 |
| 个人 Web 管理面板 | `*: allow` | 管理员完全控制 |
| 公开 Telegram Bot | `write_file: deny, exec: deny, spawn: deny` | 只读 + 对话 |
| 子 Agent 只读调研 | `spawn` 时传入 profile=`reader` | 子 Agent 只能读文件、搜索，写文件和 shell 全部拒绝 |
| 新手用户的 Web 面板 | `exec: ask` | 每次 shell 弹确认框，逐步建立信任 |

---

### 3. `restrict_to_workspace` 默认改为 True

**为什么：**

当前 `SecurityConfig.restrict_to_workspace` 默认 `False`。这意味着如果不主动去配置里打开，shell 和文件工具可以访问整个文件系统。CountBot 的定位是「面向普通用户」，默认不安全等于默认不安全。

**目标方案：**

```python
# schema.py
class SecurityConfig:
    restrict_to_workspace: bool = True  # 从 False 改为 True
```

同时在 Web UI 的「安全设置」页面显式展示此开关，首次启动时引导用户确认工作区路径。

**使用场景：**

所有默认安装部署的用户立即受益——Agent 被限制在工作区内，即使 LLM 产生幻觉写出 `rm -rf /`，路径检查也会拦截。需要更大范围访问的高级用户可以手动关闭。

---

## 🟡 P1 — 体验与质量

---

### 4. Plan Mode（规划-审批-执行）

**为什么：**

当前 Agent 收到消息后直接开始调用工具。对于简单任务（「帮我查一下天气」）这没问题，但对于复杂和高风险操作（「把所有 JS 文件重构为 TypeScript」「帮我部署到服务器」），缺少「先看再动」的中间态。

LLM 可能误解需求、遗漏关键步骤、或产生不必要的破坏性操作。Plan Mode 在「分析」和「执行」之间插入一个**人确认**的环节，把 AI 的执行力与人的判断力结合起来。

**目标方案：**

在 Workflow 和 `spawn` 中增加 `require_approval` 参数。当用户消息命中高风险关键词或显式要求规划时，Agent 进入 Plan Mode：

```
用户: "帮我把所有 JS 文件重构为 TypeScript"

Agent → Plan Mode:
  1. [只读工具] 扫描项目结构，列出所有 .js 文件
  2. [只读工具] 检查现有 tsconfig、依赖
  3. 生成计划:
     {
       "summary": "将 23 个 JS 文件迁移为 TS",
       "steps": [
         {"action": "install", "detail": "npm i -D typescript @types/node"},
         {"action": "write", "file": "tsconfig.json", "detail": "创建 TypeScript 配置"},
         {"action": "rename", "files": ["src/utils.js → src/utils.ts", ...]},
         {"action": "edit", "files": ["23 files"], "detail": "添加类型注解"}
       ],
       "affected_files": 25,
       "risk": "medium",
       "rollback": "git checkout ."
     }

前端渲染:
  ✅ npm install typescript        (安全)
  ✅ 创建 tsconfig.json            (安全)
  ⚠️ 重命名 23 个文件              (不可逆)
  ⚠️ 修改 23 个文件                (不可逆)
  📋 回滚方案: git checkout .

  共 4 步，影响 25 个文件。是否继续？

用户: "继续" → Agent 按计划执行
用户: "跳过安装步骤" → 调整后执行
用户: "取消" → 不执行
```

实现要点：
- Plan 阶段使用只读工具集（reader profile）
- 计划必须是结构化 JSON，前端可渲染为可勾选清单
- 支持步骤级跳过/调整
- 超过 N 个文件或包含高风险操作时，自动触发 Plan Mode

**使用场景：**

| 场景 | 触发方式 | 效果 |
|------|----------|------|
| 批量文件操作 | 自动（影响 > 5 个文件） | 先展示变更清单，用户确认后批量执行 |
| Workflow 团队协作 | `workflow_run` 首阶段设为 plan | 调研 agent 产出方案 → 用户审批 → 执行 agent 接力 |
| 部署操作 | 关键词触发（「部署」「上线」「发布」） | 生成部署步骤 → 确认 → 逐步执行 |
| 数据库操作 | SQL 包含 `DROP`/`ALTER`/`TRUNCATE` | 强制进入 Plan Mode，不可跳过 |
| 日常简单查询 | 不触发 | 直接执行，无额外流程 |

---

### 5. Agent 类型特化

**为什么：**

当前所有子 Agent（通过 `spawn` 或 Workflow 创建）拿到完全相同的工具集——`read/write/edit/list/exec/web_fetch`，区别仅在于 system prompt 里的文字引导。文字引导可以被 LLM 忽略，工具限制才能真正约束行为。

Workflow 的 Graph/Council 模式天然适合不同阶段的 Agent 拥有不同权限——调研阶段只需要读，执行阶段需要读写，审查阶段只需要读且应从不同角度审视。

**目标方案：**

定义 Agent 类型 Profile，每种类型绑定不同的工具集：

```python
AGENT_PROFILES = {
    "reader": {
        "tools": ["read_file", "list_dir", "web_fetch", "file_search", "wiki"],
        "system_prompt": "你是只读分析助手，只能读取和搜索，不能修改任何文件或执行命令。",
        "max_iterations": 10,
    },
    "coder": {
        "tools": ["read_file", "write_file", "edit_file", "list_dir", "exec", "web_fetch"],
        "system_prompt": "你是编程助手，可以读写文件和执行命令。",
        "max_iterations": 25,
    },
    "reviewer": {
        "tools": ["read_file", "list_dir", "file_search"],
        "system_prompt": "你是代码审查员，只能阅读代码，从安全性、正确性、性能三个维度审查。不能修改文件。",
        "max_iterations": 8,
    },
    "planner": {
        "tools": ["read_file", "list_dir", "web_fetch", "wiki"],
        "system_prompt": "你是方案规划师，负责调研现状并产出可执行的步骤计划。不能执行任何操作。",
        "max_iterations": 15,
    },
}
```

Workflow Graph 中按阶段指定类型：

```yaml
agents:
  - name: "调研"
    profile: "planner"
    depends_on: []
  - name: "实现"
    profile: "coder"
    depends_on: ["调研"]
  - name: "审查"
    profile: "reviewer"
    depends_on: ["实现"]
```

**使用场景：**

| 场景 | Agent 组合 | 效果 |
|------|-----------|------|
| 代码重构 | planner → coder → reviewer | 调研方案 → 执行修改 → 审查结果，每阶段权限递减 |
| 技术调研 | reader × 3 (parallel) | 三个只读 agent 并行搜索不同来源，互不干扰 |
| Bug 修复 | reader → coder | 只读定位根因 → 读写修复 |
| CI/CD 安全 | coder 不允许 spawn | 执行 agent 可以写代码但不能创建子 agent，防止递归失控 |

---

### 6. 结构化记忆（Memory with Metadata）

**为什么：**

当前记忆系统是单文件行式存储：

```
2026-07-01|web-chat|用户喜欢用pnpm;项目部署在阿里云
2026-07-02|auto-overflow|讨论了重构方案;决定用TypeScript
```

问题：
- **无类型区分**：用户偏好、项目知识、纠正反馈全部混在一起，检索时无法过滤
- **无交叉引用**：两条相关记忆之间没有链接，形成孤岛
- **不可增量更新**：只能追加行或按行号删除，无法更新单条记忆
- **检索粗糙**：只有关键词 AND/OR，没有按类型、时间范围、相关性的精确检索

当记忆量增长到几百条时，Agent 从 MEMORY.md 中检索到的信息质量会显著下降。

**目标方案：**

每条记忆独立为文件，带 frontmatter 元数据：

```markdown
---
name: user-prefers-pnpm
description: 用户偏好使用 pnpm 而非 npm 管理依赖
metadata:
  type: user
  created: 2026-07-01
  updated: 2026-07-02
  references:
    - project-deployed-on-aliyun
---

用户在 Node.js 项目中使用 pnpm 作为包管理器，初始化项目用 `pnpm init`，
安装依赖用 `pnpm add`。[[project-deployed-on-aliyun]]
```

记忆类型：
- `user` — 用户偏好、习惯、个人背景
- `feedback` — 用户对 AI 行为的纠正（「上次你说 X 不对，正确的是 Y」）
- `project` — 项目相关的事实（技术栈、架构决策、部署环境）
- `reference` — 外部资源（文档链接、API 地址、配置模板）

目录结构：

```
workspace/memory/
├── MEMORY.md              # 索引文件（只存文件名和一行摘要）
├── user-prefers-pnpm.md
├── project-deployed-on-aliyun.md
├── feedback-dont-use-docker-compose.md
└── ...
```

检索时按类型过滤：

```python
# 搜用户偏好
memory.search("包管理器", type="user")
# → ["user-prefers-pnpm: 用户偏好使用 pnpm"]

# 搜项目知识
memory.search("部署", type="project")
# → ["project-deployed-on-aliyun: 项目部署在阿里云 ECS"]
```

**使用场景：**

| 场景 | 当前行为 | 改善后 |
|------|----------|--------|
| 用户纠正 AI 错误 | 追加一行 `"用户说不该用docker"` | 写入 `feedback` 类型记忆，引用原始 `project` 记忆，下次检索时反馈优先级更高 |
| 跨会话记住偏好 | 混在对话总结里 | 独立 `user` 类型记忆，检索时明确标注来源 |
| 多项目管理 | 所有项目记忆混在一个文件 | 按 `project` 类型过滤，Agent 切换项目时只看相关记忆 |
| 知识图谱 | 无 | `[[交叉引用]]` 形成链接图，「用户偏好 pnpm」↔「项目 A 用 pnpm」↔「CI 配置也用 pnpm」 |

---

### 7. Workflow 对抗性验证

**为什么：**

当前 Council 模式是「多视角审议」——每个成员独立分析，然后交叉评审。但这本质上是**建设性**的（如何做得更好），而非**对抗性**的（这个方案哪里会失败）。

Harness 的 adversarial verify 模式是：对于每个发现/结论，独立 spawn 多个 skeptic agent，被明确指示「找出这个结论的问题，默认判定为不可信」。这种红队思维能显著降低 AI 幻觉和盲目乐观。

**目标方案：**

在 Council 模式中增加 `adversarial` 选项：

```yaml
mode: council
cross_review: true
adversarial_review: true    # 新增：对抗性验证
agents:
  - name: "方案分析师"
    role: "提出解决方案"
  - name: "安全审计员"
    role: "从安全角度质疑方案的每个假设"
  - name: "可靠性工程师"
    role: "找出方案可能失败的场景"
```

对抗回合的 prompt 模板：

```
你是 {role}。你的任务是**严格质疑**以下方案：
---
{proposal}
---

规则：
1. 逐条检查方案的每个假设，标记无依据的假设
2. 找出方案在极端情况下可能失败的场景
3. 不要提出改进建议——只找问题
4. 如果没有发现问题，说"未发现问题"而不是勉强凑数

返回格式：
- 致命缺陷: ...
- 可疑假设: ...
- 失败场景: ...
- 总体判断: 可信 / 存疑 / 不可行
```

**使用场景：**

| 场景 | 效果 |
|------|------|
| 安全敏感操作（部署、数据库变更） | 方案提出后经 3 个对抗 agent 质询，至少 2 票「可信」才执行 |
| 架构决策 | 多个方案各自经对抗审查，选出质疑最少的一个 |
| 自动化客服回复 | 敏感话题（退款、投诉）的回复经对抗 agent 检查是否有法律或公关风险 |
| 代码审查 | reviewer 找 bug + adversarial reviewer 故意尝试误解代码 |

---

## 🟢 P2 — 锦上添花

---

### 8. Cron 调度抖动（Jitter）

**为什么：**

当前 cron 使用精确的 `asyncio.sleep(delay)` 唤醒，所有任务都在整秒触发。如果多个 CountBot 实例部署在同一环境（或同一个 LLM API 账号下有多个定时任务），它们会在同一时刻同时调用 API，触发限流。

加上 ±30 秒的随机偏移可以平滑请求峰值，对用户体验无感知影响。

**目标方案：**

```python
# scheduler.py
import random

JITTER_SECONDS = 30

async def _arm_timer(self):
    now = self._now_shanghai()
    delay = (next_run - now).total_seconds()
    jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
    delay = max(1, delay + jitter)  # 不小于 1 秒
    await asyncio.sleep(delay)
```

**使用场景：**

企业部署 5 个 CountBot 实例，每个配置了「每天早上 9:00 发送日报」。加 jitter 后实际触发时间分布在 8:59:30 ~ 9:00:30，LLM API 不会同时收到 5 个请求。

---

### 9. 输出安全扫描

**为什么：**

当前工具执行结果直接返回给 LLM 和用户，没有任何内容扫描。如果 Agent 执行了 `cat .env` 或 `echo $API_KEY`，密钥会明文出现在对话中。

**目标方案：**

作为 `after_execute` hook 实现（依赖 P0 的 hooks 系统）：

```python
class SecretScanHook:
    PATTERNS = [
        r'sk-[a-zA-Z0-9]{32,}',           # OpenAI API key
        r'ghp_[a-zA-Z0-9]{36}',           # GitHub PAT
        r'AKIA[0-9A-Z]{16}',              # AWS Access Key
        r'Bearer\s+[a-zA-Z0-9\-._~+/]+',  # JWT / Bearer tokens
    ]

    async def after_execute(self, tool_name, params, result, context):
        for pattern in self.PATTERNS:
            result = re.sub(pattern, '***REDACTED***', result)
        return result
```

**使用场景：**

Agent 执行 `cat .env` → 输出中的 `OPENAI_API_KEY=sk-abc...` 被替换为 `OPENAI_API_KEY=***REDACTED***`。

---

### 10. 配置分层

**为什么：**

当前配置只有一层（数据库 `Setting` 表），加上运行时的 session 级覆盖。缺少中间层（如渠道级、团队级默认值），导致管理多机器人部署时需要大量复制粘贴配置。

**目标方案：**

参考 Harness 的 merge 策略：

```
Session 覆盖（最高优先级）
  ↓
团队配置（AgentTeam 的 model_config）
  ↓
渠道默认值（如飞书用便宜的模型，Web 用强的模型）
  ↓
全局默认值（AppConfig）
```

**使用场景：**

一个实例同时服务飞书客服（需要便宜快速的模型）和 Web 管理面板（需要最强模型做复杂任务）。渠道级默认值让两个入口自动使用不同配置，无需每个会话手动调整。

---

### 11. Sandbox 选项（容器隔离）

**为什么：**

当前 shell 执行使用 `asyncio.create_subprocess_shell()`，子进程与宿主共享文件系统、网络和用户权限。路径检查是字符串匹配，不是真正的隔离——symlink、shell 变量展开、命令替换都可能绕过。

对于面向公网用户的部署，这是一个潜在风险点。

**目标方案：**

作为可选增强，不改变默认行为。在 `SecurityConfig` 中增加：

```python
class SecurityConfig:
    sandbox_mode: str = "none"  # "none" | "docker" | "firejail"
    sandbox_image: str = ""     # Docker image（sandbox_mode=docker 时）
```

当 `sandbox_mode="docker"` 时，`exec` 工具通过 `docker run --rm -v workspace:/workspace:rw ...` 执行命令，获得真正的文件系统和网络隔离。

**使用场景：**

公有云上部署面向外部用户的 CountBot 实例，开启 Docker sandbox 确保即使 LLM 被 prompt injection 诱导执行恶意命令，也无法突破容器边界。

---

### 12. Context 缓存感知

**为什么：**

LLM 提供商（Anthropic、OpenAI 等）的 prompt cache 通常有 5 分钟 TTL。当前 CountBot 的上下文压缩和 cron 调度没有考虑缓存失效，导致每次唤醒都重新发送完整的系统提示词，增加延迟和成本。

**目标方案：**

- 上下文构建时，将稳定的系统提示词部分（角色定义、技能列表、工具定义）放在前缀，使其命中 cache
- Cron 高频任务（< 5 分钟间隔）保持底层连接和上下文预热
- 后台上下文维护时，只修改变化的部分（新增消息），不变前缀保持 cache 热度

**使用场景：**

用户连续与 Agent 对话 10 轮，每轮间隔 3 分钟。由于系统提示词前缀未变，LLM 提供商的 prompt cache 持续命中，每轮节省 ~80% 的 prompt 处理成本。

---

### 13. 子 Agent 结构化输出

**为什么：**

当前子 Agent（`spawn`、Workflow 各阶段）返回自由文本。主 Agent 或后续阶段需要解析这些文本才能提取结构化信息（文件列表、决策结论、风险评估），解析过程不可靠且浪费 token。

**目标方案：**

`spawn` 和 workflow agent 支持 `output_schema` 参数：

```python
result = await subagent_manager.execute_task(
    task="分析项目结构和依赖关系",
    profile="reader",
    output_schema={
        "type": "object",
        "properties": {
            "language": {"type": "string"},
            "framework": {"type": "string"},
            "files_count": {"type": "integer"},
            "dependencies": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["language", "framework", "files_count"]
    }
)
# result 是验证过的 dict，不是自由文本
```

**使用场景：**

Workflow Graph 中，「调研」agent 产出结构化项目信息 → 「实现」agent 直接消费结构化数据，无需从文本中重新解析。

---

## 实施优先级总结

```
P0 (必须做，解锁后续):
  ├── 1. Hooks 系统         ← 基础设施，所有扩展的插入点
  ├── 2. 工具权限分级        ← 安全的必要条件
  └── 3. restrict_to_workspace 默认 True  ← 一行改动，影响最大

P1 (应该做，投入产出比高):
  ├── 4. Plan Mode          ← 减少 AI "莽撞"操作
  ├── 5. Agent 类型特化      ← 让 Workflow 权限可控
  ├── 6. 结构化记忆          ← 让长期记忆真正可用
  └── 7. 对抗性验证          ← 提升结论可靠性

P2 (可以做，锦上添花):
  ├── 8. Cron Jitter
  ├── 9. 输出安全扫描        ← 依赖 P0 hooks
  ├── 10. 配置分层
  ├── 11. Sandbox 选项
  ├── 12. Context 缓存感知
  └── 13. 子 Agent 结构化输出
```

## 与现有架构的关系

这些优化不需要推倒重来。CountBot 现有的基础设施已经足够坚实：

- **ToolRegistry** → 增加 hook 链和权限检查器
- **SubagentManager** → 增加 agent profile 和 output schema
- **WorkflowEngine** → 增加 plan 阶段和 adversarial_review 参数
- **MemoryStore** → 从行式迁移到文件式，保持 `append/search/delete` 接口兼容
- **CronScheduler** → 在 `_arm_timer` 中加 jitter
- **SecurityConfig** → 增加新字段和默认值修改

每一项都是增量改造，不破坏现有功能。
