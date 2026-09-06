# Agent 设计审计提示词（基于 Armin Ronacher《Agent Design Is Still Hard》）

> 来源：https://lucumr.pocoo.org/2025/11/21/agents-are-hard/ （2025-11-21）
> 目标：用这篇文章的论点系统审计 **CountBot main 分支（62486dd）**，找出与"现代 agent 工程实践"的差距与可改进点。
> 用法：在下面的审计工作目录里开一个 agent 会话，**一次只贴一个 prompt**，按顺序执行。每个 prompt 都是两阶段：**先只读检查、出报告、停下等确认，再改代码**。

## ⚠️ 与旧版的区别（务必先读）

旧版三个 prompt 已存档为 `archive/audit-prompts-rag-branch.md`，它针对 `feature/rag-enhancement` 分支的 RAG 链路（`modules/rag/`、`rag-bench/`）。
**本版针对 main 分支**：main 上**没有** `modules/rag/`、**没有** `rag-bench/`，只有 `modules/wiki/`（BM25）。两者的检查对象完全不同，不要混用。

### 已预先核实的 main 分支事实（2026-09-03，可直接作为线索起点，但仍需执行时复验行号）

| 事实 | 位置 |
|---|---|
| 系统提示词把**当前时间**拼进去了（`datetime.now()`） | `agent/context.py:297`（`build_system_prompt` 在 `:93`） |
| 用户称呼 / 用户身份也拼进系统提示词 | `context.py:303`、`:312`、`:321 user_lines`、`:375 user_info` |
| **正面样本**：team_reminder 追加到首条用户消息（后置注入，正确做法） | `context.py:549-561`（append 到 `messages[0]["content"]`） |
| 工具集运行时可变（`unregister` 存在） | `tools/registry.py:147 register` / `:164 unregister` / `:207 list_tools` / `:272 get_definitions` |
| **无任何 prompt caching**：`cache_control`/`cache` 在 anthropic provider 里为空 | `providers/anthropic_provider.py`（1052 行，零 cache 命中） |
| 不支持 provider-side tools（无 web_search / server_tool） | `providers/anthropic_provider.py`、`openai_provider.py` 均无 |
| Provider 抽象极薄（只有 84 行左右：ToolCall / StreamChunk / LLMProvider） | `providers/base.py:9`、`:17`、`:53`、`:71 chat_stream`、`:84` |
| **无 output tool 概念**（output/respond/reply/final_answer 全为空） | 全 `tools/` 目录无命中 |
| 存在"跳过工具直接作答"路径 | `agent/loop.py:244`、`:517`、`:605`、`:612` `direct_result_selected` |
| 唯一的弱 reinforcement（仅超限触发） | `loop.py:618-626` `yield warning_msg` |
| 子 agent 有 FAILED/CANCELLED 状态与 result/error 双字段 | `agent/subagent.py:19-20`、`:48-49`、`:56 to_dict` |
| 子 agent 工具超时文案合格（一句话 + 原因） | `subagent.py:519`；结果截断 500 字符在 `:523` |
| wiki 10 个动作全是"读写 wiki 自身"，结果无法落盘 | `wiki/tool.py:47-50`（enum: search/ask/get/batch_get/list/stats/create/update/delete/sync） |
| 共享文件层存在（workspace + temp 目录） | `workspace/manager.py:24 workspace_path()`、`:31 temp_dir()` |
| 审计日志骨架已存在（可作 eval 数据源） | `tools/file_audit_logger.py:18`、`:21 max_days=30`、`:62 record_call`、`:99`、`:139`、`:229`、`:248` |
| 测试几乎为空（tests/ 仅 1 个文件，无 eval 体系） | `tests/test_context_service_workflow_metadata.py` |
| 上下文压缩未实现 | `agent/compactor.py` **0 字节** |

### 语境差异提醒（不要盲目照搬 Armin 的结论）

Armin 的 agent 是**后台任务型**（跑完发一封邮件，中间过程对用户不可见），CountBot 是**对话式助手**（接飞书/Web，用户实时看流式输出）。
因此：
- "用显式 output tool 替代自由文本输出"这条**不能照搬**。CountBot 的正解更可能是：保留流式输出，但补上"循环结束未产出有效动作时的 reinforcement"，而不是引入一个 output tool。
- 其余（缓存前缀、失败隔离、共享层、eval）**与形态无关，可直接适用**。

---

## Step 0：准备工作（执行一次）

main 分支没有独立工作目录，**先建一个审计专用 worktree**，避免污染 main 和你现有的两个分支：

```bash
# 从 main 开出审计分支，工作目录独立于现有两个 worktree
git -C /Users/daniel/project/countbot worktree add -b audit/armin-main /Users/daniel/project/countbot-audit main

# 验证：确认 root 指向新目录、分支正确、内容与 main 一致
git -C /Users/daniel/project/countbot-audit rev-parse --show-toplevel
git -C /Users/daniel/project/countbot-audit branch --show-current
git -C /Users/daniel/project/countbot-audit status --short | head
```

后续所有 prompt 的工作目录均为 **`/Users/daniel/project/countbot-audit`**。
不用时清理：`git -C /Users/daniel/project/countbot worktree remove /Users/daniel/project/countbot-audit`

---

## 执行顺序

| 顺序 | Prompt | 主题 | 类型 | 理由 |
|---|---|---|---|---|
| 1 | 缓存前缀稳定性 | 让前缀可缓存 | 纯检查 + 明确违规 | 已确认有违规，最快出成果；且是后续缓存工作的前提 |
| 2 | Provider 层与显式缓存 | 要不要接 prompt caching | 架构决策 | 承接 #1，先稳定前缀再谈加 cache point |
| 3 | 共享层与死胡同 | 工具产出能否互相消费 | 事实认定为主 | 结论明确，改动可大可小 |
| 4 | 失败隔离 | 失败输出不污染上下文 | 文案与返回协议 | 中等改动，风险低 |
| 5 | Output 与 reinforcement | 循环收尾保障 | 设计决策 | 涉及是否引入新机制，需你拍板 |
| 6 | Eval 可观测性 | 怎么衡量改动有效 | 最难 | Armin 自己承认没找到满意方案，放最后 |

---

## Prompt 1：缓存前缀稳定性

```text
你是 CountBot 项目的资深工程师。工作目录：/Users/daniel/project/countbot-audit（分支 audit/armin-main，基于 main 62486dd）。
本次任务只读、不改任何代码。

【原文依据】
Armin：*"Because the system prompt and the tool selection now have to be mostly static, we feed a dynamic message later to provide information such as the current time. Otherwise, this would trash the cache."*
LLM 服务商的 prompt caching 按【前缀】匹配：请求从第 1 个 token 起与上次完全一致的部分可大幅降价；前缀中任何一处变化，其后所有缓存全部失效。
两条工程约束：① 系统提示词与工具定义必须静态，不能含当前时间、用户身份、会话状态、检索结果等每轮/每用户会变的内容；② 动态信息必须以后置消息（工具结果、附加消息）进入，而不是写进系统提示词或工具 description。

【已知线索（需你复验行号，可能已漂移）】
- `backend/modules/agent/context.py:93` `build_system_prompt`；`:297` 疑似 `datetime.now().strftime(...)` 拼进系统提示词
- `context.py:303`/`:312` user_name、`:321` user_lines、`:375` user_info
- `context.py:512` `build_messages`
- 正面样本 `context.py:549-561`：team_reminder 追加到 `messages[0]["content"]`（首条用户消息后置注入，这是正确做法，作为改造模板）

【检查清单】
1. 定位系统提示词的**实际组装位置**（`context.py` 为主，也要看 `prompts.py`、`personalities.py`、skills 相关文件），逐行确认组装结果里混入了哪些动态内容。
2. 逐项排查动态源：当前时间/日期、用户名或 user_id、会话摘要/记忆条目、人格(persona)切换、渠道(web/飞书/CLI)差异、工作区路径、工具列表、随机元素、环境变量。
3. 检查工具定义是否静态：`backend/modules/tools/registry.py:272 get_definitions` 的产出是否会因运行时状态变化（注意 `:164 unregister` 与 `:147 register` —— 工具集增减会直接改变前缀）。再查 `backend/modules/agent/skills*.py` 是否有运行时拼接变量进工具 description。
4. 检查 messages 序列是否 append-only：`backend/modules/agent/compactor.py` 当前是**空文件**（0 字节），确认是否因此完全没有上下文压缩；若其他地方有压缩/重写历史，标注触发频率与位置。
5. 顺带确认：`context.py:549-561` 的 team_reminder 注入方式是否只在特定条件触发（条件触发 = 前缀不稳定，因为它改变了首条消息）。

【交付物】审计报告（不改代码）
- 违规清单：每条 = 文件:行号 + 违规的动态内容 + 它为什么会变 + 修复建议（应移到哪个后置位置）
- 灰色地带清单：看起来违规但有理由的，说明权衡
- 总体结论：当前系统提示词前缀是否稳定；若不改造直接上 prompt caching，预计能命中多少

完成后停下，输出报告，等我确认后再进入修改阶段。

【修改阶段约束（等我确认后执行）】
- 动态内容从系统提示词移到：首条用户消息的附加信息 / 工具结果 / 循环中的强化消息，选对现有流程侵入最小的一种
- 系统提示词静态部分一次构造、复用
- 最小改动，不破坏现有渠道（飞书/Web/CLI）行为
```

---

## Prompt 2：Provider 抽象层与显式缓存能力

```text
你是 CountBot 项目的资深工程师。工作目录：/Users/daniel/project/countbot-audit（分支 audit/armin-main）。
本次任务只读、不改任何代码。这个审计会引出架构决策，把选项和利弊摆清楚，不要替我做决定。

【原文依据】
Armin 的结论是**不要用高层 SDK 抽象（如 Vercel AI SDK），直接用平台原生 SDK**，三个理由：
① 模型差异足够大，你必须自建 agent 抽象，而 SDK 给的不是你想要的那一层；
② provider-side tools 的统一消息格式行不通（他举 Anthropic web search 破坏消息历史为例）；
③ 缓存管理直接打原生 SDK 更简单，错误信息也更清晰。
关于缓存他的立场转变值得注意：*Anthropic 让你为缓存付费、让你显式管理 cache point，一开始觉得很蠢，现在完全转而偏好显式管理——成本和命中率可预测得多。* 做法是：系统提示词后 1 个 cache point，对话开头 2 个，最后一个随对话尾部上移。

【已知线索（需复验）】
- `backend/modules/providers/base.py` 抽象极薄（约 84 行）：`ToolCall:9`、`StreamChunk:17`、`LLMProvider:53`、`chat_stream:71`、`get_default_model:84`
- `providers/anthropic_provider.py` 1052 行，但 `cache_control`/`cache` 命中为 **0**；`providers/openai_provider.py` 同理
- 两个 provider 均无 `web_search`/`server_tool`/`provider_tool` 命中 → 不支持 provider-side tools
- 相关文件：`factory.py`、`registry.py`、`runtime.py`、`thinking_profiles.py`、`tool_parser.py`

【检查清单】
1. 评估抽象层的**厚度是否合适**：`base.py` 只有 5 个符号，这个抽象是否薄到"模型差异泄漏到了上层"？举出具体泄漏点（如 thinking/reasoning 处理、tool_call 结构、错误语义、流式协议）。
2. 反过来检查：抽象是否又厚到"挡住了模型原生能力"？重点看有没有因为统一格式而丢掉 Anthropic 独有的能力（cache_control、extended thinking 的签名校验、provider-side tools）。
3. **缓存能力盘点**：确认是否真的零缓存支持；若为零，评估接入 `cache_control` 需要改哪些点（请求构造、消息数组、计费/命中率观测）。
4. Provider-side tools：确认当前不支持。分析"不支持"的代价（如无法用 Anthropic 原生 web search，只能用 `backend/modules/tools/web.py`）与好处（规避了 Armin 说的消息历史被破坏问题）。给出你的判断：这个取舍在当前阶段是否合理。
5. 错误信息可读性：对比两个 provider 的错误分类与文案（已知 `openai_provider.py:746 _is_auth_error()`、`:198-200` 认证错误不重试）。Armin 特别看重"错了能看懂"。

【交付物】审计报告（不改代码）
- 抽象层评估：泄漏点清单 + 被挡住的原生能力清单，各附文件:行号
- 缓存接入方案（若决定接）：需要改的点、预计收益、风险；给 2-3 个由小到大的选项
- Provider-side tools 取舍判断
- 明确标注：哪些是"必须改"，哪些是"可以不做"

完成后停下，输出报告，等我选方案后再动。
```

---

## Prompt 3：共享层与死胡同

```text
你是 CountBot 项目的资深工程师。工作目录：/Users/daniel/project/countbot-audit（分支 audit/armin-main）。
本次任务只读、不改任何代码。

【原文依据】
Armin：*"You should try to build an agent that doesn't have dead ends. A dead end is where a task can only continue executing within the sub-tool that you built."*
他举的例子：图像生成工具只能把图喂给某一个下游工具，于是你没法用代码执行工具把这批图打包成 zip。解法是所有工具通过一个共享层（他用的是虚拟文件系统）交换数据，且**必须双向**——代码执行解包 zip → 推理描述图片 → 再回到代码执行。
判断一个交换点是否开放，问三个问题：
① 这个产出除了当前设定的下游，还能被谁读到？
② 明天要加一条新链路（A 的产出 → 新工具 C），要不要改 A 的代码？
③ 产出有没有稳定可引用的标识（文件路径、id），让下游能精确取用？

【已知线索（需复验）】
- 共享层：`backend/modules/workspace/manager.py:24 workspace_path()`、`:31 temp_dir()`、`:86 prepare_workspace_path`、`:95 activate_workspace_path`
- 工具：`tools/filesystem.py`、`shell.py`、`screenshot.py`（`:72 output_path` 参数、`:143 _capture_desktop`）、`send_media.py`（`:39 file_paths` 参数，直接发到 channel）
- 疑似死胡同：`wiki/tool.py:47-50` 的 10 个动作（search/ask/get/batch_get/list/stats/create/update/delete/sync）**全是读写 wiki 自身**，检索结果只回到模型上下文，无落盘出口
- 已知：wiki 是整篇文档级索引，零切分（`wiki/service.py:196` 一文件一 doc），检索返回的是格式化文本

【检查清单】
1. 画出工具间的数据流（文字版）：标注每个工具的**输入形态**与**产出形态**（文件路径 / 结构化对象 / 纯文本回模型），以及产出落在哪里。
2. 对每对"产出 → 消费"关系回答上面三个问题，找出全部死胡同。重点核查：
   a. `screenshot.py` 产出的图片，能否被 `shell.py` 打包/被 `send_media.py` 发送？（看产出是 base64 字符串还是可引用路径）
   b. `wiki` 检索结果能否写入文件、或被其他工具作为参数消费？（若不能，"检索到的内容 → 落盘 → 交给代码处理"这条链就是断的）
   c. `tools/web.py` 抓取的内容（已知 `web.py:181 max_chars=50000` 截断）能否落盘供后续工具使用？
   d. `send_media.py` 是否是单向终点（发出去即消失，无返回值可引用）？
3. 检查 workspace 是否真的是**统一**共享层：各工具解析路径的基准是否一致（相对路径 vs 绝对路径、是否都走 `WorkspaceManager`）。若有工具绕过 workspace 自己拼路径，标出来。
4. 检查子 agent 场景：`tools/spawn.py` + `agent/subagent.py` 派出去的子任务，它的产出能否落到同一个共享层供主循环读取（Armin 强调这点对 subagent 尤其重要）。

【交付物】审计报告（不改代码）
- 数据流图（文字版）
- 死胡同清单：每个 = 位置(文件:行号) + 被堵住的具体组合场景（要举真实会遇到的任务，如"截图 10 张打包发给用户"）+ 候选开放方案
- 路径一致性问题清单
- 三个候选方案按改动从小到大排列，说明利弊与影响面，等我选

完成后停下，输出报告。
```

---

## Prompt 4：失败隔离

```text
你是 CountBot 项目的资深工程师。工作目录：/Users/daniel/project/countbot-audit（分支 audit/armin-main）。
本次任务只读、不改任何代码。

【原文依据】
Armin：*"If you expect a lot of failures during code execution, there is an opportunity to hide those failures from the context."* 两种做法：
① 把可能需要反复迭代的任务放进 subagent 跑，只回报成功 + 一段"哪些路子走不通"的摘要——*他强调 agent 需要知道什么没成功，才能在下一次绕开，但不需要失败的完整输出*；
② context editing 把没推动进展的失败从上下文里剔除（代价是失效缓存，trade-off 尚不明确）。
核心原则：**失败的完整细节给人看（logger/落盘），一句话总结给模型看，且总结里要带"下一步该怎么办"。**

【已知线索（需复验）】
- 主循环工具执行：`agent/loop.py:729 execute_tool`；工具层 `tools/registry.py:299 execute`
- 参数解析失败处理：`registry.py:217 _extract_tool_argument_parse_failure`、`:235 _format_tool_argument_parse_error`
- 合格样本：`agent/subagent.py:519` 工具超时返回 `"Error: Tool '{name}' execution timed out after {n} seconds. The tool may be stuck or the operation is taking too long."`（一句话 + 原因推断）
- 子 agent 状态：`subagent.py:19 FAILED`、`:20 CANCELLED`、`:48 result`、`:49 error`、`:220` 超时文案
- 结果截断：`subagent.py:523` `result[:500]`
- 上下文压缩未实现：`agent/compactor.py` **0 字节**（意味着"剔除失败"这条路当前不存在）

【检查清单】
枚举 main 分支工具层的全部失败模式，逐个追踪"当前实际返回给模型的内容"：
1. 工具不存在（幻觉工具名，已知 `registry.py` 有兜底日志）
2. 参数解析失败 / 参数缺失 / 类型错误
3. 工具执行抛异常（重点看 `shell.py`、`filesystem.py`、`web.py`、`screenshot.py` 的 except 分支）
4. 工具超时（对比 `subagent.py:519` 的合格文案，主循环里是否同样处理）
5. 权限/路径越界拒绝
6. 外部依赖缺失（命令不存在、文件不存在、网络失败）
7. 子 agent 失败（FAILED/CANCELLED/TIMEOUT）时，回传给主循环的是什么
对每种失败做三类分类：
- **A 类（合格）**：一句话总结 + 下一步建议
- **B 类（污染）**：把堆栈 / 超长 JSON / 原始异常文本返回给模型
- **C 类（危险）**：直接抛异常打断 agent 循环
另外检查两点：
8. 失败总结是否保留"教训"：比如"文件不存在"有没有告诉模型下一步怎么办（先列目录？换路径？）
9. `registry.py:299 execute` 的统一入口是否对所有工具一视同仁，还是各工具自行 try/except 导致风格不一

【交付物】审计报告（不改代码）
- 失败模式表：失败类型 | 位置(文件:行号) | 当前返回给模型的原文摘要 | 分类(A/B/C) | 建议的一句话文案（含下一步建议）
- B、C 类按修复优先级排序，标注每条的修复成本
- 指出"是否有统一失败返回协议"，若无，给出协议设计建议

完成后停下，输出报告，等我确认后再进入修改阶段。

【修改阶段约束（等我确认后执行）】
- 统一失败返回协议：logger 记完整细节（人看）+ 返回模型一句话总结（含下一步建议）
- 不改变正常路径的行为
- 以 `subagent.py:519` 的文案风格为基准统一
```

---

## Prompt 5：Output 与 reinforcement（循环收尾保障）

```text
你是 CountBot 项目的资深工程师。工作目录：/Users/daniel/project/countbot-audit（分支 audit/armin-main）。
本次任务只读、不改任何代码。**这是设计决策，把选项和利弊摆清楚，不要替我做决定。**

【原文依据】
Armin 的 agent 不代表 chat session，用一个显式 output tool 与外界通信（他的场景是发邮件）。他踩到两个坑：
① **语气措辞极难控制**——试过让子模型（Gemini 2.5 Flash）润色语气，结果增加延迟、降低质量，还会泄露中间步骤；
② **模型有时就是不肯调用这个 tool**——解法是记住 output tool 是否被调用过，**循环结束时若未调用，就注入一条 reinforcement message 强制它调用**。
他还提到"we also leverage reinforcement during the loop much more"（循环过程中也大量用强化消息）。

【语境差异：不要照搬，务必先读这段】
Armin 是**后台任务型 agent**（跑完发一封邮件，中间过程对用户不可见），所以需要 output tool 来"产出最终物"。
CountBot 是**对话式助手**（接飞书/Web，用户实时看流式输出）。引入 output tool 会破坏流式体验，大概率是错的。
因此本审计的正确命题是：**不引入 output tool 的前提下，如何保障循环收尾的可靠性？**

【已知线索（需复验）】
- **无 output tool 概念**：全 `backend/modules/tools/` 下 output/respond/reply/final_answer/submit 命中为 0
- **存在"跳过工具直接作答"路径**：`agent/loop.py:244`、`:517`、`:605`、`:612` `direct_result_selected`（`process_direct` 在 `:761`）
- **唯一的弱 reinforcement**：`loop.py:618-626`，仅在达到 `max_iterations`/工具调用上限时 `yield warning_msg`（内容形如 `[达到最大迭代次数 N]`）
- **无任何常规 reinforcement**：loop.py 中 reinforce/强化/reminder/nudge 命中为 0
- 主循环上限：`loop.py:45 max_iterations=25`、`:255 while`
- 已知关联缺口：主循环**没有"必须先调工具"门禁**（模型可以完全不调工具直接作答）

【检查清单】
1. 确认"无 output tool"这一事实，并理解 CountBot 的产出路径：最终文本如何到用户（`loop.py` 的 yield `final_content` → API → 渠道）。画出这条链路。
2. 枚举**循环异常收尾**的全部情形，逐个看当前会给用户什么：
   a. 达到 max_iterations 但任务明显未完成
   b. `direct_result_selected` 触发（模型跳过工具直接作答）——此时是否告知用户"未执行任何操作"？
   c. 所有工具调用均失败后循环结束
   d. 被 cancel_token 中断（`loop.py:258`、`:408`）
   e. 工具调用预算耗尽（`loop.py:353-366` 整批截断、`:401`）
3. 对照 Armin 的"循环结束未调用 output tool → 注入 reinforcement"，给出 CountBot 的等价命题：**哪些情形下应该注入强化消息而不是直接收尾？** 给出具体触发条件与建议文案。
4. 评估引入常规 reinforcement 的收益与风险：会不会打断对话式体验？在哪些场景下值得（如"该用工具却没用"）？
5. 检查 `loop.py:618-626` 现有的 warning 文案是否足够（用户看到 `[达到最大迭代次数 25]` 能理解发生了什么吗？知道下一步该做什么吗？）

【交付物】审计报告（不改代码）
- 循环收尾路径图 + 全部异常收尾情形表（情形 | 位置 | 当前给用户的反馈 | 是否足够）
- 建议的 reinforcement 触发条件清单（按优先级排序），每条附建议文案
- 明确回答：在当前对话式形态下，是否应该引入 output tool？（给出你的判断与理由）
- 列出"不做什么"（例如：不引入 output tool、不用子模型润色语气）

完成后停下，输出报告，等我选方案后再动。
```

---

## Prompt 6：Eval 可观测性（怎么证明改动有效）

```text
你是 CountBot 项目的资深工程师。工作目录：/Users/daniel/project/countbot-audit（分支 audit/armin-main）。
本次任务只读、不改任何代码。

【原文依据】
Armin 把 testing/evals 列为**最难的问题，且承认自己没找到满意方案**：*"Unlike prompts, you cannot just do the evals in some external system because there's too much you need to feed into it. This means you want to do evals based on observability data or instrumenting your actual test runs."*
关键洞察：**agent 的 eval 不能脱离真实运行数据**，必须基于可观测数据或对真实测试运行打点。

【已知线索（需复验）】
- **测试几乎为空**：`tests/` 下仅 `test_context_service_workflow_metadata.py` 一个文件，无 eval 体系
- **审计日志骨架已存在**：`tools/file_audit_logger.py:18 FileAuditLogger`、`:21 max_days=30`、`:62 record_call`、`:99 update_result`、`:139 record_ai_response`、`:229 get_logs_by_session`、`:248 get_stats`
- 工具执行统计：`tools/registry.py:467 get_stats`
- 注意对比：`feature/ddggkkcc` 分支上有 `tests/hallucination/`（evaluator.py 等），但**本分支（main）没有**——不要跨分支引用

【检查清单】
1. 盘点现有可观测能力：`file_audit_logger.py` 实际记录了哪些字段？粒度够不够支撑 eval？（工具名/参数/耗时/成功与否/token 数/会话 id？）
2. 检查统计接口（`registry.py:467 get_stats`、`file_audit_logger.py:248 get_stats`）产出什么指标，是否可导出。
3. 确认缺失项：有没有 token 计量、成本归因、缓存命中率观测、工具失败率、迭代次数分布？逐项给"有/无 + 证据行号"。
4. 评估：以现有埋点为骨架，做一套最小可用 eval 需要补什么？给出"打点 → 采集 → 指标 → 回归对比"的最小闭环设计。
5. **特别针对前面 5 个审计的改动**：如果按 Prompt 1（缓存前缀）做了改造，怎么证明它有效？（提示：需要能测量"缓存命中率"或"每轮请求的首个变化 token 位置"）。把"如何衡量"写进方案，不要只写"如何改"。
6. 判断数据保留策略：`max_days=30` 是否够做趋势分析？落盘格式是否便于离线分析？

【交付物】审计报告（不改代码）
- 现有可观测能力盘点表（能力 | 位置 | 字段/指标 | 是否够用）
- 缺失项清单（按"阻碍哪个审计结论的验证"归类）
- 最小可用 eval 闭环设计（打点 → 采集 → 指标 → 回归对比），附需要新增的埋点位置
- **衡量方案**：前 5 个审计的每类改动，分别用什么指标证明有效

完成后停下，输出报告。
```

---

## 附：通用纪律（六个 prompt 都适用）

1. **先只读，后动手**：任何 prompt 的第一阶段都不改代码，出报告后停下等确认。
2. **报告必须带文件:行号**：不带位置的发现等于没发现。我给的线索行号可能已漂移，**执行时必须复验**。
3. **改动最小化**：审计分支 `audit/armin-main` 是实验场，但结论最终要能回馈主线，任何改动不得破坏现有功能（飞书/Web/CLI 三渠道行为一致）。
4. **⚠️ macOS grep 陷阱**：BSD grep 的 `\|` **不是**或运算，会静默返回 0 条（伪阴性）。必须用 `grep -E "a|b"`。本项目已因此误判过两次。
5. **断言"没有 X"前必须换同义词复搜**（如 cache/lru/memo、去重/dedup/幂等、reinforce/强化/reminder/nudge）。单次检索为空不足以断言缺失。
6. **区分"事实"与"判断"**：报告里的事实要能复核（行号），判断要明确标注是你的推断，不要把 Armin 的观点当成必须照做的结论——语境不同（他是后台任务型 agent，CountBot 是对话式助手）。
