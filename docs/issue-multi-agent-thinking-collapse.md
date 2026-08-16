# Issue: 多智能体协作最终输出未折叠思考过程

> **状态**: 待修复  
> **优先级**: P1 — 影响核心阅读体验  
> **类型**: UX / 前端渲染  
> **关联日志**: `data/audit_logs/audit_2026-08-03_0bc1d1e5.log`、`data/logs/CountBot_2026-08-03.log`

---

## 一、问题概述

在多智能体协作（`workflow_run`）模式下，每个子成员的思考过程虽然在工作流面板内可以折叠，但**最终汇总输出**将所有子智能体的 `## 思考过程` 原样渲染为 Markdown 文本，未继承折叠行为，导致输出冗长、可读性差。

**关键矛盾**：单 Agent 路径的思考过程已通过 `ReasoningBlock` 组件折叠；工作流面板（`WorkflowListPanel` / `WorkflowComparePanel`）内也已正确折叠；**唯独最终 AI 响应文本的渲染路径遗漏了折叠逻辑。**

---

## 二、根因定位

### 2.1 后端：子 Agent 将思考过程嵌入结果文本

**文件**: `backend/modules/agent/subagent.py`，第 97-109 行

```python
@staticmethod
def _compose_reasoning_sections(reasoning_text: str, visible_text: str) -> str:
    """在不支持独立 reasoning 面板的子代理链路中保留 reasoning 内容。"""
    normalized_reasoning = str(reasoning_text or "").strip()
    normalized_visible = str(visible_text or "").strip()

    if not normalized_reasoning:
        return normalized_visible

    sections = [f"## 思考过程\n\n{normalized_reasoning}"]   # ← 思考过程被嵌入为 Markdown 标题
    if normalized_visible:
        sections.append(f"## 回复\n\n{normalized_visible}")
    return "\n\n---\n\n".join(sections)
```

第 428-431 行调用此方法，第 559 行 `task.result = "".join(response_chunks)` 将包含 `## 思考过程` 的文本作为最终结果。

### 2.2 后端：工作流引擎拼接结果时未去除思考过程

**文件**: `backend/modules/agent/workflow.py`

三种模式（`run_pipeline` / `run_graph` / `run_council`）都将子 Agent 的 `task.result`（含 `## 思考过程`）原样拼接到最终 Markdown：

- `run_pipeline` 第 326-331 行
- `run_graph` 第 457-474 行
- `run_council` 第 576-595 行

其中 `run_council` 第 594 行甚至显式指示主 Agent「请将以上每位成员的完整分析内容如实呈现给用户，不要省略或替换为简短摘要」，进一步放大了问题。

### 2.3 后端：主循环将工作流结果直接作为最终响应

**文件**: `backend/modules/agent/loop.py`，第 515-520 行

```python
if tool_name == "workflow_run" and prefer_direct_workflow_result:
    final_content = result          # ← 工作流结果直接作为最终响应
    direct_result_selected = True
    if result:
        yield result               # ← 直接 yield 给前端
    break
```

此路径在用户消息中提及团队名称时触发（`chat.py` 第 1049-1053 行）。

### 2.4 前端：最终响应渲染路径未调用拆分逻辑

**文件**: `frontend/src/modules/chat/MessageItem.vue`

`extractWorkflowDisplayText()`（第 739-763 行）只去除 `WORKFLOW_EXEC` 元数据注释，**不处理** `## 思考过程` 标题：

```typescript
const extractWorkflowDisplayText = (content, toolCalls) => {
  const stripped = stripWorkflowMeta(content)  // ← 只去除 HTML 注释
  // ... 只处理 Pipeline 格式的最后一个 block ...
  return stripped  // ← ## 思考过程 标题原样返回
}
```

`displayContent`（第 784-795 行）和 `renderedContent`（第 797-803 行）最终用 `renderMarkdown()` 直接渲染：

```typescript
const renderedContent = computed(() => {
  const content = displayContent.value
  if (props.message.role === 'assistant') {
    return renderMarkdown(content)  // ← ## 思考过程 被渲染为普通 Markdown 标题
  }
  return content
})
```

**此处没有调用 `splitReasoningSections`，没有使用 `ReasoningBlock`** — 这就是 Bug 的直接表现。

---

## 三、数据流全景

```
子 Agent LLM 流式响应
  │
  ├─ reasoning_buffer + content_buffer 分离收集 (subagent.py L395-422)
  │
  └─ _compose_reasoning_sections() 将思考过程嵌入为 ## 思考过程 Markdown (subagent.py L97-109)
       │
       └─ task.result = "## 思考过程\n\n[推理]\n\n---\n\n## 回复\n\n[回复]" (subagent.py L559)
            │
            └─ WorkflowEngine 拼接所有 Agent 结果 (workflow.py L326-331 / L457-474 / L576-595)
                 │
                 └─ workflow_run 返回最终 Markdown 字符串 (workflow_tool.py L244-248)
                      │
                      ├─ [路径A] prefer_direct_workflow_result → 直接作为最终响应 (loop.py L515-520)
                      │
                      └─ [路径B] 作为 tool result，主 Agent 再生成响应 (loop.py L522-535)
                           │
                           └─ 前端 MessageItem.vue 渲染
                                │
                                ├─ displayContent → extractWorkflowDisplayText (不拆分思考过程)
                                ├─ renderedContent → renderMarkdown() (直接渲染为 Markdown)
                                └─ ❌ ## 思考过程 被原样输出，未折叠
```

---

## 四、已有能力对比

| 路径 | 思考过程是否折叠 | 机制 |
|------|:-:|------|
| **单 Agent 主循环** | ✅ 是 | `loop.py` 分离 `reasoning_buffer` → 独立 SSE `reasoning` 事件 → 前端 `ReasoningBlock` 折叠 |
| **工作流面板内（每个 Agent）** | ✅ 是 | `WorkflowListPanel` / `WorkflowComparePanel` 调用 `splitReasoningSections` + `ReasoningBlock` |
| **最终 AI 响应（汇总输出）** | ❌ 否 | `MessageItem.vue` 直接 `renderMarkdown`，未拆分思考过程 |

**核心组件复用关系**：
- `splitReasoningSections()` — `frontend/src/utils/reasoningSections.ts` 第 18 行，按 `## 思考过程` / `## 回复` 拆分文本
- `ReasoningBlock.vue` — `frontend/src/components/chat/ReasoningBlock.vue`，可折叠组件
- 两者**已在工作流面板中使用**，但**未在最终响应渲染中使用**

---

## 五、修复方案

### 方案 A：前端修复（推荐，改动最小，见效最快）

在 `MessageItem.vue` 中，当检测到消息关联了 `workflow_run` 工具调用时，对 `displayContent` 调用 `splitReasoningSections` 进行拆分：

1. `reasoning` 部分 → 通过 `ReasoningBlock` 渲染（默认折叠）
2. `content` 部分 → 通过 `renderMarkdown` 渲染

**改动文件**: `frontend/src/modules/chat/MessageItem.vue`  
**改动范围**: `displayContent` / `renderedContent` computed 属性，模板中增加 `ReasoningBlock` 条件渲染  
**复用已有**: `splitReasoningSections` + `ReasoningBlock`，无需新建组件

**伪代码**:
```typescript
const reasoningSections = computed(() => {
  if (!hasWorkflowRun.value) return null
  return splitReasoningSections(displayContent.value)
})

// 模板中：
<ReasoningBlock
  v-if="reasoningSections?.reasoning"
  :content="reasoningSections.reasoning"
  :default-expanded="false"
/>
<div v-html="renderMarkdown(reasoningSections?.content || displayContent)" />
```

### 方案 B：后端修复（根因修复，但改动范围大）

在 `subagent.py` 中，当子 Agent 被工作流引擎调用时，不在 `task.result` 中嵌入思考过程，而是通过独立字段（如 `task.reasoning`）传递。`WorkflowEngine` 拼接最终结果时只使用 `content` 部分。

**改动文件**:
- `backend/modules/agent/subagent.py` — 分离 reasoning 和 content
- `backend/modules/agent/workflow.py` — 拼接时只取 content
- `backend/modules/agent/loop.py` — 需确保 reasoning 仍可通过 SSE 事件传递

**风险**: 需要修改数据结构 `SubagentTask`，影响面较大；需确保工作流面板的实时流式显示（`event_callback` 发送 `## 思考过程` chunk）不受影响。

### 方案 C：后端替代修复（折中）

在 `WorkflowEngine` 的 `run_pipeline` / `run_graph` / `run_council` 拼接最终结果时，使用正则去除各 Agent 输出中的 `## 思考过程` 段落，只保留 `## 回复` 部分。

**改动文件**: `backend/modules/agent/workflow.py`  
**风险**: 会丢失思考过程信息，用户无法事后展开查看；需评估是否需要保留。

### 推荐组合

**方案 A（前端）+ 方案 C（后端保留可选）**：
- 方案 A 解决最终响应渲染问题，复用已有组件，改动最小
- 方案 C 可作为后端兜底，在拼接时提供 `include_reasoning` 选项控制是否保留思考过程

---

## 六、受影响文件清单

### 后端

| 文件 | 行号 | 角色 |
|------|------|------|
| `backend/modules/agent/subagent.py` | 97-109, 428-431, 559 | 思考过程嵌入结果文本（根因） |
| `backend/modules/agent/workflow.py` | 326-331, 457-474, 576-595 | 工作流结果拼接 |
| `backend/modules/agent/loop.py` | 515-520 | 工作流结果直接作为最终响应 |
| `backend/modules/tools/workflow_tool.py` | 162-253 | `workflow_run` 工具入口 |

### 前端

| 文件 | 行号 | 角色 |
|------|------|------|
| `frontend/src/modules/chat/MessageItem.vue` | 739-763, 784-803 | **最终响应渲染（未折叠）** |
| `frontend/src/utils/reasoningSections.ts` | 18 | `splitReasoningSections()` — 已有工具 |
| `frontend/src/components/chat/ReasoningBlock.vue` | 全文 | 可折叠组件 — 已有 |
| `frontend/src/components/chat/WorkflowListPanel.vue` | 51-56, 103-114 | 工作流面板（正确折叠） |
| `frontend/src/components/chat/WorkflowComparePanel.vue` | 57-62, 112-122 | 工作流面板（正确折叠） |
| `frontend/src/composables/useReasoningDisplay.ts` | 全文 | 全局折叠偏好 |

---

## 七、验证步骤

1. 配置多智能体团队（如 council 模式，2+ 个 Agent）
2. 发起对话触发 `workflow_run`
3. 查看最终输出：
   - **修复前**: 所有 `## 思考过程` 原样展开，内容冗长
   - **修复后**: 各 Agent 思考过程默认折叠为摘要，仅展示 `## 回复` 内容，用户可点击展开
4. 确认工作流面板内的折叠行为不受影响
5. 确认单 Agent 路径的折叠行为不受影响



