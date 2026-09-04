# [Enhancement] 为 Wiki 知识库与记忆引入可插拔语义检索（RAG 增强）

> 用途：GitHub issue 底稿。正文可直接复制提交；文末附录为验收 golden set。
> 提交后此文件属于个人工作文件，可移出仓库或删除。

## 一句话摘要

CountBot 现有的检索能力（Wiki 的 BM25 整篇文档召回 + 记忆的行式子串搜索）是所有用户都会遇到的通用瓶颈——长文档打爆上下文、语义变体召回不到、答案无出处。本提案把检索能力升级为**分块 + 词法/语义混合 + 可溯源引用**，分阶段落地、零破坏兼容，并用**官方文档**作为可复现的验收语料。

## 现状与问题（为什么值得做）

CountBot 官方定位是"面向本地部署、私有业务和持续运行场景的 AI Agent 运行环境"，记忆与知识库是其长期能力核心（官方文档 core/memory、核心模块 wiki）。但当前检索均为**词法级**：

1. **记忆系统**（`backend/modules/agent/memory.py`）：行式子串搜索（官方文档明示 `search` 为"关键词搜索"）。语义相关但字面不同的内容召回不到——用户问"我的开发环境偏好"匹配不上已存的"我使用 zsh"。
2. **Wiki 知识库**（`backend/modules/wiki/`，v0.9.0）：BM25 全文检索 + **整篇文档召回**。长文档（单篇可达数十 KB）会整篇塞入上下文：token 开销大、噪声高、跨文档信息无法整合、答案无出处可溯。

三个用官方文档即可复现的具体失败示例：

| 用户问题 | 期望 | 现状 |
|---------|------|------|
| "记忆存储在哪个文件？" | 召回 memory 文档的"存储位置"小节 | 命中整篇 memory 文档全文注入，或子串漏检 |
| "桌面版为什么不能远程访问？" | 召回 installation 文档中桌面版限制的 warning 段落 | 需整篇检索才能找到一句话，噪声大 |
| "外部编程工具怎么接入？" | 跨多个页面整合（channels / external-coding-tools / 配置手册） | 逐篇关键词试，无法一次整合 |

这些都是**任何部署 CountBot 的用户**都会问的通用问题，与具体业务无关。

## 方案概览（怎么做）

### 架构：一个模块、一条管线、按语料形态分模式

新增 `backend/modules/rag/`，向下一条统一摄取管线（分块 → 嵌入 → BM25 + 向量双索引，mtime + content_hash 增量同步），向上按**语料形态**（而非按业务）提供可插拔模式：

| 模式 | 适用语料形态 | 检索策略 |
|------|------------|---------|
| A 精准问答 | 长文档知识库（官方文档、产品手册、日志） | 分块 + BM25/向量混合（RRF）+ 重排，带引用溯源 |
| B 语义记忆 | 短期/长期记忆条目 | 写入即异步嵌入 + 向量召回 + 时间衰减 |
| C 时序聚合 | 周期性流入的多源内容（RSS、渠道消息） | 时间窗 + 语义去重 + 来源可靠性加权 |
| D 生成辅助 | 静态技能/范例（SKILL.md 等，<500K token 时） | 长上下文直读即可，不强制上检索（后置） |

**可插拔 = 三个插槽**：分块策略（摄取端）、Retriever 策略（检索端）、生成模板（生成端），由语料注册表 `workspace/rag/corpora.yaml` 配置驱动、调用时可覆盖。新增一种语料 = 一段 YAML + 必要时一个 Retriever 实现，不新建系统。

### 与现有架构的兼容性（零破坏）

- RAG 以 **Tool 形式注册进现有 ToolRegistry**，AgentLoop 零改动——由 LLM 自主决定何时检索（Agentic RAG），不需要引入任何 Agent 编排框架。
- 增量同步继承 WikiService 现有 mtime 机制，加 content_hash 双校验。
- 嵌入源走现有 LiteLLM provider 体系（云端/本地 Ollama 可切换，不可用时 Noop 降级，不中断检索）。
- **memory/news/wiki 存储格式全部不变**：向量池是**可删除重建的派生索引**，不影响任何现有数据与行为。
- 零新微服务、零新框架依赖；向量检索默认 faiss-cpu / Chroma 本地文件，符合"本地部署、私有环境优先"定位。

### 实施路线（P1–P4，每阶段可独立合并）

- **P1** 分块 + BM25 分块级召回（零新依赖，先解上下文爆炸与噪声）
- **P2** 向量 + RRF 混合（语义补全，BM25 命不中的同义表达）
- **P3** 语义记忆（写入即嵌入 + 时间衰减）
- **P4** 时序聚合（去重 + 来源加权）

每阶段附独立验收标准（见下），可拆成多个 PR 逐步合入。

## 验收方式（怎么证明做对了——作者可复现）

以**官方文档（countbot.cn/docs，约 15+ 页）**为验收语料——所有 contributor 可访问、可复现、可审核。golden set 问题全部出自官方文档，每题标注预期要点与出处（附录 A）。

每阶段门禁（改动前后跑同一批题，数据对比）：

| 阶段 | 门禁标准 |
|------|---------|
| P1 | ① 同批问题上下文 token 显著下降 ② 召回的是相关块而非整篇 ③ ≥70% 答案可溯源到具体块 |
| P2 | ① 比纯 BM25 多命中正确答案 ≥3 题 ② 若向量无提升→该语料用不上向量，停在此级（有效结论）③ 重排后精度不降 |
| P3 | ① 跨会话召回"字面不同、语义相关"偏好 ② 近期记忆优先 ③ 误召回率低 |
| P4 | ① 跨源重复被合并 ② 个性化命中偏好 ③ 来源权重生效 |

验收文档与测试随 PR 提供（golden set 落为 `tests/` 下可执行用例 + 对比脚本）。

## 范围（不做什么）

- 不引入 LangChain / LlamaIndex 等重框架（本项目本地单机定位，抄模式不抄依赖）。
- 不引入独立向量库服务（Pinecone/Milvus/Qdrant 均不需要；本地 faiss-cpu 即可）。
- 不做 GraphRAG：公开 benchmark 显示其对多跳推理 +27 分、对通用问答仅 +0.47 分，本项目语料场景无收益。
- 不改变现有存储格式与 API；向量池仅为派生索引。
- 不把检索硬编码进 AgentLoop（检索仍是 Tool，由模型决策）。

## 参考

- 官方定位：https://countbot.cn/docs/
- 现状代码：`backend/modules/wiki/`（BM25 整篇召回）、`backend/modules/agent/memory.py`（子串搜索）
- 已有内部方案文档：`archive/rag-enhancement-plan.md`（Wiki 单点，已归档）、`architecture.md`（总体设计，含市场调研依据）、`test-plan.md`（可量化测试方案）、`milestones.md`（里程碑与验收标准）

---

## 附录 A：golden set（20 题，全部出自官方文档，可复现）

> 用法：改动前后对同一批题各跑一遍，记录 token、溯源率、答案质量；每题答案须能在对应官方页面找到出处。

### 记忆系统（docs/core/memory）

1. Q：CountBot 的长期记忆存储在哪个文件？
   A：`workspace/memory/MEMORY.md`　出处：core/memory「存储位置」
2. Q：记忆条目的一行格式是什么？
   A：`日期|来源|内容`　出处：core/memory「存储位置」
3. Q：记忆的来源（source）可能有哪些？
   A：web-chat / telegram / dingtalk / feishu / cron / auto-overflow 等　出处：core/memory「存储位置」
4. Q：会话历史超出窗口限制时系统会做什么？
   A：找出超窗旧消息 → 生成摘要 → 以 auto-overflow 来源写入记忆　出处：core/memory「自动总结」
5. Q：MemoryStore 提供哪些方法？
   A：append_entry / search / read_lines / get_recent / get_stats　出处：core/memory「MemoryStore」

### 安装与部署（docs/getting-started/installation）

6. Q：CountBot 支持哪几种运行方式？
   A：源码运行（start_app.py）、桌面模式（start_desktop.py）、Docker　出处：installation
7. Q：环境要求是什么？
   A：Python 3.11+；构建前端或文档需 Node.js 18+（可选）　出处：installation「环境要求」
8. Q：如何让局域网其他设备访问 CountBot？
   A：设置 `COUNTBOT_HOST=0.0.0.0`（可加 `COUNTBOT_PORT`）　出处：installation「启动服务」
9. Q：桌面预编译版（.exe 安装包）支持远程访问吗？
   A：不支持——预编译版无法修改监听地址，需用源码版　出处：installation 桌面模式 warning
10. Q：requirements.txt 中 Web 框架类包含哪些依赖？
    A：fastapi、uvicorn、websockets　出处：installation「安装依赖」依赖分类表
11. Q：启动后自检清单包含哪些项？
    A：Web 可打开 / 模型配置可读写 / 基础问答可用 / 工作空间可读写 / 渠道测试通过 / 日志无 ERROR　出处：installation「启动后自检清单」
12. Q：工作空间（workspace/）承载哪些内容？
    A：技能目录 skills/、记忆文件 memory/、临时文件与截图 temp/、业务产物、external_coding_tools.json　出处：installation「确认工作空间」

### 系统结构与工具（docs/core/*）

13. Q：lifespan 启动流程中核心模块的初始化顺序大致是什么？
    A：SQLite → 配置 → ProviderRegistry → MemoryStore/SkillsLoader/ToolRegistry/SubAgentManager/AgentLoop → 队列与限流 → ChannelManager → 渠道连接 → CronScheduler 等　出处：installation「lifespan 初始化流程」
14. Q：CountBot 的目录结构中前端构建产物放在哪里？
    A：`frontend/dist/`（Release 包已包含）　出处：installation「目录结构」/「构建前端」
15. Q：LLM 提供商层（ProviderRegistry）注册了多少个 provider？
    A：22 个　出处：installation「lifespan 初始化流程」

### 场景与配置（docs/getting-started/*、docs/scenarios/*）

16. Q：首次进入后官方建议的配置顺序是什么？
    A：模型 → 工作空间 → 人格 → 团队/渠道/编程工具　出处：installation「首次进入后的建议顺序」
17. Q：如何测试模型连接是否成功？
    A：`POST /api/settings/test-connection`　出处：installation「配置模型」
18. Q：CountBot 面向哪些使用场景？（官方定位）
    A：本地/私有网络运行可控 Agent；多角色多渠道协作；聊天/编程/检索/任务/记忆串成完整链路　出处：docs 首页「适合哪些场景」
19. Q：智能体团队支持哪些协作模式？
    A：pipeline / graph / council　出处：docs 首页「产品结构」
20. Q：CountBot 可以把哪些外部编程工具接入渠道？
    A：Claude、Codex、OpenCode 等 CLI 工具，可在 IM 渠道账号设置 AI 模式或直通模式默认路由　出处：docs 首页「外部编程工具」/ core/external-coding-tools

---

## 附录 B：一句话给维护者的答复

> 如果对必要性有疑问：用上面 golden set 任一题在现有版本跑一次即可复现——要么整篇灌入上下文，要么子串漏检。本提案只是把"检索"这件 Agent 的核心能力从词法级补到语义级，且每一步都可用官方文档验收。
