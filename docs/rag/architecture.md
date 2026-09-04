# CountBot RAG 增强方案：市场调研驱动的架构设计

> 状态：设计文档 v2（替代"场景驱动"初稿的叙事结构；架构内核沿用并经受市场证据校验）。
> 方法论：先调研市场（成熟方案 → 各自场景 → 生产公司实践 → 趋势共识），再倒推 CountBot 该做什么、不做什么。
> 配套文档：`test-plan.md`（可量化测试方案，含必要性/可行性实验与 P1–P4 门禁）、`issue-proposal.md`（issue 底稿）、`milestones.md`（里程碑与验收标准）。早期实施计划 `archive/rag-enhancement-plan.md`（选型表/chunk schema/风险清单如需回溯可查）。

---

## 0. 结论先行（TL;DR）

1. **2026 年市场共识已经变了**：naive RAG（分块→嵌入→top-k→拼 prompt）死了；**检索正在从"管道"变成"工具"**——LLM 自己决定何时检索、读结果、判断证据够不够、不够再检索。这是对 CountBot 最有利的判断：**AgentLoop + ToolRegistry 架构天然就是这个模式**，RAG 增强只需要把检索能力做成工具，不需要动主循环。
2. **"向量库优先"被市场推翻**：Claude Code 砍掉了 embedding 管线改用 grep/glob；业界明确了长上下文与 RAG 的分界线（有界小语料 <500K token 长上下文直接赢；RAG 不可替代的四大理由 = 超大规模、查询成本、新鲜度、权限控制）。**CountBot 的 BM25 不是落后，是混合检索的合法一半**——市场证明生产级 RAG 的默认形态是 BM25+向量混合（hybrid），我们要做的是"补齐另一半"，不是推倒重来。
3. **市场成熟模式的场景分类非常清晰**（Perplexity 的多级检索管线 / Notion 的页面级分块+权限即过滤 / Cursor 的结构感知分块+Merkle 增量 / Glean 的个人上下文画像 / 生产标配的元数据过滤），CountBot 的四类真实语料各命中其中一种，一一对应四种可插拔检索模式。
4. **该不做的同样来自市场证据**：GraphRAG（多跳 +27 分 vs 通用问答 +0.47 分，CountBot 无多跳需求）、独立向量数据库（"vector DB first" 正在退潮，个人语料规模 numpy 即可）、以及"把检索硬编码进流程"（与 agentic 趋势背道而驰）。

---

## 1. 市场调研

### 1.1 框架层格局（开源方案 → 各自场景）

| 框架 | 定位 | 最佳场景 | GitHub Stars（2026-01） |
|------|------|---------|------------------------|
| **LangChain / LangGraph** | 编排框架 | 通用 Agent + 多步 agentic RAG 工作流 | ~125K |
| **Dify** | 低代码平台 | 非技术团队快速搭建知识库应用 | ~114K |
| **RAGFlow** | 深度文档理解引擎 | PDF/扫描件/表格等复杂文档 QA、GraphRAG | ~70K |
| **LlamaIndex** | 数据框架 | 数据密集型 RAG、海量连接器、agentic 索引 | ~46.5K |
| **Haystack** | 生产管线框架 | 演示→生产的可靠过渡、带评估 | ~24K |
| txtai / R2R / LightRAG / Cognita / Pathway | 细分领域 | 轻量嵌入库 / agentic RAG API / 简单场景 / 模块化部署 / 流式实时索引 | 小众 |

**格局要点**：①"RAG 框架"已是四类不同物种（编排库 / 端到端平台 / 深度解析引擎 / 评估工具），按场景选类而不是按星数选型；② 框架解决的是**企业级通用问题**（多租户、权限、可视化、连接器），而 CountBot 是**本地单机个人运行时**——框架层的价值主要是"抄模式"，不是"装依赖"。

### 1.2 成熟公司的生产实践（本方案的主要依据）

这一层比框架层重要——它们代表经过生产验证的**模式**：

**① Perplexity：多级检索管线（搜索型产品）**
向量+关键词双路召回 ~50 候选 → rerank 模型精排 → 上下文融合引擎把每个生成论断映射回源文档出引用。启示：**混合检索 + 重排 + 强制引用**是搜索型产品的标准形态。

**② Notion AI：页面级分块 + 权限即检索过滤（知识库产品）**
分块以页面为边界（不跨页切块）；**权限在检索时执行**（只返回用户有权限的 chunk）；页面变更时增量更新嵌入；语义+关键词混合。启示：**分块尊重数据自然边界、元数据过滤先于相似度**。

**③ Cursor：结构感知分块 + Merkle 树增量 + 隐私脱敏（代码场景）**
tree-sitter AST 按函数/类切分（非固定 token）；嵌入+混淆路径存云端向量库（Turbopuffer），源码不出本机；**每 5-10 分钟 Merkle 树对比指纹，只重索引变更文件**；查询时语义检索 + grep/ripgrep 混合。启示：**结构感知分块、增量索引的工程化、混合词法+语义是代码场景标配**。

**④ Claude Code（2026-02）：砍掉 embedding 管线，改用 grep/glob（agentic 检索）**
Anthropic 把 Claude Code 的向量库+嵌入管线**整体移除**，换成 agentic 实时 grep/glob 搜索。原因："模型比相似度函数更擅长决定找什么"——管道检索跑一次就盲出结果，agent 能读了结果发现不对再换关键词。这个模式随后在编码 agent 中扩散。**两个注意**：代码是词法检索的极端有利场景（有精确标识符）；学界对此仍有分歧（多轮 agentic 检索非单调更优）。启示：**检索是工具不是管道；词法检索在特定场景是正解而非妥协**。

**⑤ Glean / 个人助手类（Obsidian/Mem）：个人上下文画像**
跨 SaaS 连接器的企业搜索，或个人笔记的语义召回——把"用户是谁、偏好什么"沉淀为可检索资产，按当前任务语义召回注入。启示：**记忆的语义化是个人 Agent 的核心增值点**。

### 1.3 2026 年趋势判断（多条独立来源交叉验证）

| 趋势 | 证据 | 对本方案的影响 |
|------|------|--------------|
| **检索从管道变工具（Agentic Retrieval）** | Claude Code 移除向量库改 grep/glob；RAGBible "State of RAG mid-2026"：naive RAG 已死 | CountBot 的 ToolRegistry 路线被市场验证为正确方向 |
| **Hybrid（BM25+向量）是严肃生产 RAG 默认形态** | dev.to 生产指南："Hybrid is the default in serious production RAG by 2026"；OpenLLM 生产 API 用 BM25+语义+RRF | 保留并升级 BM25 为分块级，补向量一路 |
| **长上下文 vs RAG：互补而非替代** | 确定性语料 <500K token（约 37.5 万字）长上下文几乎总是更优；RAG 四大不可替代：超大规模、查询成本（省 50-200×token）、高频更新、检索时权限 | CountBot 语料量级判定见 §2.2 |
| **Context Rot（上下文腐烂）** | 有效上下文 << 标称窗口；1M 满窗口时 1/4 多针检索任务失败；128K-256K 是信息密度最优区间 | 不能靠"反正窗口大就塞全文" |
| **GraphRAG 定位收窄** | "Do We Still Need GraphRAG?"：多跳任务 +27.23 分，通用 QA 仅 +0.47 分 | CountBot 无跨文档多跳推理需求 → 不做 |
| **评估必做，否则盲飞** | 多来源：50-200 条 golden set 每次变更回归；RAGAS/DeepEval 生态成熟 | 每个模式配 golden set，进 `tests/` |
| **重排在生产普及** | Perplexity 用 rerank 从 50 候选选最终集；bge-reranker/Qwen3-Reranker 开源成熟 | Phase 后期可选项 |

### 1.4 市场成熟模式的场景分类（调研归纳，本方案的"需求来源"）

| # | 成熟模式 | 代表 | 适用场景特征 |
|---|---------|------|------------|
| 1 | Agentic 词法检索 | Claude Code | 有精确标识符的语料（代码/文件名），agent 自主迭代搜索 |
| 2 | 混合检索+重排+引用 | Perplexity、Notion | 长文档知识库问答，需要精度与可溯源 |
| 3 | 结构感知分块+增量索引 | Cursor | 频繁变更的语料（代码/笔记），索引需实时性 |
| 4 | 个人上下文画像 | Glean、Obsidian/Mem | 偏好/事实的语义召回，注入任务上下文 |
| 5 | 元数据过滤+新鲜度加权 | 生产 RAG 标配 | 流式时序语料（新闻/工单/邮件），时间窗+来源过滤先于相似度 |
| 6 | 范例检索注入生成端 | 生成型产品实践 | 生成任务的知识注入（设计范例/模板库） |
| 7 | 长上下文直读 | 2026 反 RAG 潮 | 有界小语料（<500K token）的单文档/小集合问答 |

---

## 2. CountBot 的需求定位（从市场倒推）

### 2.1 CountBot 是什么：本地优先的个人 AI Agent 运行时

不是企业知识库平台（Dify/RAGFlow 的场景），不是搜索产品（Perplexity），也不是 IDE（Cursor）。它是**单机、私有、持续运行**的 Agent 底座：AgentLoop + ToolRegistry + 技能系统 + 行式记忆 + Cron 调度。这个定位直接筛掉大量"市场方案"：多租户权限、可视化编排、企业连接器——**都不需要**。

### 2.2 CountBot 的真实语料盘点（代码级审计结论）

| 语料 | 规模与特征 | 命中的市场模式 |
|------|-----------|---------------|
| `workspace/wiki/concepts/` | 个人知识文档，长文档为主 | 模式 2（混合检索+引用）；现状是 BM25 整篇召回 |
| `interview-digest/` | 26+ 份长文档，持续增长，单份 ~80KB | 模式 2 + 模式 3（增量索引）；整篇召回打爆上下文 |
| `memory/MEMORY.md` | 行式记忆，日增，条目短 | 模式 4（个人上下文画像）；现状是子串搜索 |
| news RSS 流 | 20+ 源持续流入 | 模式 5（元数据过滤+新鲜度）；现状是关键词过滤 |
| 技能知识（SKILL.md） | 有界、静态为主 | 模式 7（长上下文直读足够）；范例库增长后再考虑模式 6 |
| 本地文件/代码 | shell/grep 工具已有 | 模式 1（agentic 词法检索）已具备，零改动 |

**长上下文 vs RAG 的诚实判定**：wiki 当前总量 <500K token，理论上长上下文直读可行——但 CountBot 是**持续运行的 Agent，不是单次问答**：AgentLoop 里每轮都带上下文，token 成本随轮数累积；context rot 使 128K-256K 之外的信息密度劣化；且面经语料还在增长。结论：**检索做在查询时点、按需取片段，是成本与质量的双重要求**，与市场"混合模式"判断一致。

### 2.3 需求清单（从市场模式 × 语料盘点推导）

| 需求 | 来源 | 优先级 |
|------|------|--------|
| R1 长文档分块级混合检索+引用 | 模式 2 × wiki/面经 | **P0** |
| R2 结构感知分块 + mtime+hash 增量 | 模式 3 × 变更语料 | **P0**（与 R1 同批） |
| R3 记忆写入即嵌入、语义召回 | 模式 4 × memory | P1 |
| R4 流式语料时间窗+语义去重 | 模式 5 × news | P1 |
| R5 检索以 Tool 注册、agent 自主调用 | 模式 1 趋势 × AgentLoop | **P0 且是架构原则** |
| R6 golden set 评估回归 | 市场共识 | 与 R1 同批 |
| R7 范例检索注入（生成端） | 模式 6 × 技能知识 | P2 后置 |
| R8 重排 | Perplexity 实践 | P2 可选 |

---

## 3. 方案设计

### 3.1 总体架构：一条摄取管线 × 四种可插拔模式 × 一个工具入口

```
AgentLoop（不变）：LLM 自主决定何时调用检索 = Agentic RAG（市场已验证方向）
   │ ToolRegistry
   ▼
RagTool（统一工具入口：search / ask / stats / sync，corpus 参数选择语料）
   │
   ▼
模式层（Retriever 接口，插槽② 按语料配置、调用可覆盖）
   Mode A 精准问答    Mode B 语义记忆    Mode C 时序聚合    Mode D 生成辅助
   （HybridRRF）      （SemanticRecency）（TemporalDedup）  （ExemplarTopK）
   │
   ▼
摄取管线（增量：mtime + content_hash）
   chunker.py（插槽① 分块策略：标题层级/行式/条目式/结构式）
   → embeddings.py（嵌入源可切换：LiteLLM 云端 / Ollama bge-m3 / Noop 降级）
   → stores/（BM25 分块级 + 向量 numpy/faiss + 元数据）
```

### 3.2 四种模式详解（每种都锚定市场依据）

#### Mode A · 精准问答（Hybrid Chunk RAG）— 依据：Perplexity/Notion 实践

- **适用**：wiki、interview-digest 等长文档静态语料的问答与查找（需求 R1/R2）。
- **检索策略**：查询 → BM25 分块级 top-20 + 向量 top-20 → RRF 融合（`Σ 1/(60+rank)`）取 top-8 → 可选 rerank 精排 top-4（R8，Phase 2+ 可选）。
- **生成策略**：问答模板 + 强制引用（`[slug#heading]`），提示词要求"无依据则如实说"。对齐 Perplexity 的论断→源映射与"上下文不足就承认"的生产守则。
- **分块策略**（插槽①）：Markdown 标题层级 + overlap，每片携带 heading_path——对齐 Notion"分块尊重自然边界"与 Cursor"结构感知切分"，面经按题号/标题天然可切。
- **数据流转**：`文件 → chunker → 双索引（mtime+hash 增量）→ 查询时双路召回+RRF → 片段+引用 → LLM`。

#### Mode B · 语义记忆（Semantic Memory）— 依据：Glean/个人助手模式

- **适用**：行式记忆的语义召回（需求 R3）。记忆是"个人上下文画像"，不是知识库——它的消费者是**任务提示**而非问答。
- **检索策略**：写入即异步嵌入（不阻塞写路径）；读取时"当前任务/对话 → 嵌入 → 向量 top-k + 子串兜底合并 → 时间衰减 `0.5^(Δdays/30)`"；记忆类型分 `preference`（不衰减）/ `fact` / `event`（快衰减）。
- **生成策略**：不直接生成答案——召回结果**注入任务系统提示**（早报注入偏好、邮件分拣注入规则），由上层 LLM 消费。
- **数据流转**：`memory.append → 异步 embed → 记忆向量池（派生索引，MEMORY.md 仍是唯一真相源，可删可重建）→ 任务触发时召回 → 注入提示`。
- **降级**：嵌入源不可用时退回现有子串搜索，行为不变。

#### Mode C · 时序聚合（Temporal Aggregation）— 依据：生产 RAG 元数据过滤标配

- **适用**：news RSS 流式语料（需求 R4）。特征：持续追加、强时效、多源重复、需跨时间回溯。
- **检索策略**：入池时带 `timestamp/source/category/reliability` 元数据（URL hash 幂等防重入）；查询时**时间窗过滤（元数据先行）→ 向量召回 → 语义去重（余弦 >0.85 聚簇，簇内取最高可靠性源）→ 可靠性加权排序（官方 3×/媒体 2×/社区 1×）→ top-N**；滚动淘汰 90 天外条目。
- **生成策略**：聚合摘要模板——去重后条目清单（含来源/时间）→ LLM 结构化摘要，同簇多源合并叙事并注明多源验证。
- **数据流转**：`news 技能拉取 → 条目 embed 入池 → 查询（时间窗+向量+去重+加权）→ 条目清单 → LLM 聚合`。现有 `--keyword` 过滤保留作粗筛。

#### Mode D · 生成辅助（Exemplar Retrieval）— 依据：生成端 RAG 实践（后置）

- **适用**：web-design 等生成型技能的知识注入（需求 R7，P2）。SKILL.md 静态规范目前 <500K token，长上下文直读够用（市场模式 7 判定）；**仅当规范/范例库增长到静态注入不经济时启用**。
- **检索策略**：任务描述 → 嵌入 → 召回 top-3 范例结构骨架 + 命中的规范片段，只注入需要的部分。
- **生成策略**：`[任务] + [范例骨架×3] + [相关规范] + [硬性约束]` 注入模板。
- **数据流转**：范例库（结构化 md）→ embed 入池 → 任务时召回 → 注入生成提示 → 分段 write_file。

### 3.3 可插拔架构（模式间如何解耦）

三个插槽 + 一份语料配置表：

- **插槽①（摄取端）分块策略**：`HeadingChunker`（A）/ `LineChunker`（B）/ `ItemChunker`（C）/ `StructureChunker`（D）——统一 `Chunker` Protocol；
- **插槽②（检索端）Retriever 策略**：`HybridRRF` / `SemanticRecency` / `TemporalDedup` / `ExemplarTopK`——统一 `Retriever` Protocol，filters 透传（时间窗/类型/来源）；
- **插槽③（生成端）上下文组装模板**：问答引用 / 记忆注入 / 聚合摘要 / 范例注入——统一 `Assembler`；
- **语料注册表** `workspace/rag/corpora.yaml`：每个语料声明 source/chunker/retriever/embed/extras；`RagTool` 支持调用时覆盖 mode。**新增一种场景 = 一段 YAML + （必要时）一个 Retriever 实现**。

```python
# backend/modules/rag/ 模块结构
corpus.py          # CorpusRegistry + CorpusConfig
chunker.py         # Chunker Protocol + 4 实现（插槽①）
embeddings.py      # EmbeddingProvider：LiteLLM / Ollama / Noop 降级
stores/            # bm25_store（复用 wiki/index.py）+ vector_store（numpy/faiss）+ metadata
retriever.py       # Retriever Protocol + 4 模式实现（插槽②）
assembler.py       # 上下文组装器 + 4 生成模板（插槽③）
pipeline.py        # 摄取编排：mtime+hash 增量 → 分块 → 嵌入 → 入库
tool.py            # RagTool：注册进 ToolRegistry（插槽外唯一入口）
```

### 3.4 与现有模块的集成（改动清单）

| 模块 | 改动 | 市场依据 |
|------|------|---------|
| `wiki/tool.py` | `ask`/`search` 委托 RAG 层，CRUD 保留 | Perplexity 模式 |
| `wiki/service.py` | force_sync 接 pipeline（分块+嵌入） | Cursor 增量模式 |
| `agent/memory.py` | append 后挂异步嵌入钩子，search 加语义路 | Glean 模式 |
| `skills/news/` | 拉取后写 news_pool（URL hash 幂等） | 生产元数据过滤标配 |
| `skills/web-design/` | P2 后置：SKILL.md 瘦身 + 范例库 | 长上下文判定 |
| AgentLoop / ToolRegistry / Cron | **零改动** | agentic retrieval 趋势 |

### 3.5 实施路线（需求优先级 → 阶段）

| 阶段 | 内容 | 市场依据 | 验收 |
|------|------|---------|------|
| P1 分块 + BM25 分块级 | chunker + 分块索引 + WikiTool.ask 取片段 | "简单按段落分块会丢 40% 关键信息"→结构分块 | 上下文 80KB → ≤4KB；零新依赖 |
| P2 向量 + 混合 + golden set | embeddings + vector_store + RRF + 评估集 | hybrid 是 2026 默认 | 同义查询命中率 > 纯 BM25（golden set 回归） |
| P3 Mode B 语义记忆 | memory 嵌入钩子 + SemanticRecency | Glean 模式 | 偏好变体表述可召回 |
| P4 Mode C 时序聚合 | news_pool + TemporalDedup | 元数据过滤标配 | 跨源去重 + 回溯查询可用 |
| P5 Mode D 生成辅助 | 范例库 + 注入模板 | 后置验证 | 生成 token 注入下降 |
| P6 重排 + 前端面板 | 可选 bge-reranker + Wiki 面板模式切换 | Perplexity 实践 | top50→top5 精度提升 |

### 3.6 评估与可观测（市场共识：不做等于盲飞）

- 每语料 20-50 条 golden set（查询→期望命中），P2 起变更必回归，进 `tests/`；
- 指标：召回率@k、MRR、faithfulness（轻量自评脚本起步，不强制 RAGAS）；
- 检索日志：`{query, corpus, mode, hits, scores, latency}` 落 `data/`，loguru；
- 嵌入源降级事件 WARN 级告警（降级链：Ollama → LiteLLM → Noop 纯 BM25）。

---

## 4. 不做什么（每条都有市场证据）

| 不做 | 证据 |
|------|------|
| **不上独立向量数据库服务** | "vector DB first" 2026 被推翻；个人语料规模 numpy 足够，运维成本不匹配 |
| **不把检索硬编码进 AgentLoop** | 检索正在从管道变工具；硬编码把 Agent 退化成管道 |
| **不推倒 BM25 重来** | hybrid 是生产默认形态，BM25 是合法的一半；Claude Code 甚至纯词法 |
| **不做 GraphRAG** | 多跳 +27 分但通用 QA 仅 +0.47 分；CountBot 无多跳需求证据 |
| **不引入 LangChain/LlamaIndex 重框架** | 它们解决企业通用问题；单机个人运行时引入 = 为生态税买单 |
| **不搞统一大 prompt** | 各模式生成模板分离，是各场景生产验证的沉淀 |
| **不追"向量库都不要"的营销叙事** | "vectorless RAG" 是病毒式营销模板，无可复现结果，仅代码场景词法占优 |

---

## 5. 面试叙事（2 分钟版）

"我给 CountBot 做 RAG 增强时，第一步不是选向量库，而是做市场调研。2026 年的行业共识是 naive RAG 已经死了，检索正从管道变成工具——Claude Code 甚至砍掉了 embedding 管线改用 agentic grep。这对 CountBot 是个好消息：我们的 AgentLoop + ToolRegistry 架构天然就是 agentic retrieval 的宿主。

调研出的成熟模式我归成七类：Perplexity 的多级检索管线（混合召回+重排+引用）、Notion 的页面级分块和检索时权限、Cursor 的 AST 结构分块和 Merkle 树增量、Glean 式个人上下文画像、生产标配的元数据过滤、生成端范例检索、以及小语料长上下文直读。CountBot 的四类语料各命中其中一种，所以方案是：一条摄取管线（结构感知分块+可切换嵌入+双索引）、三个插拔点（分块/检索/生成模板）、四种模式按语料配置、统一一个 RagTool 入口注册进工具表，AgentLoop 零改动。

同样来自市场证据的是'不做什么'：GraphRAG 多跳 +27 分但通用问答只 +0.47 分，我们没这个需求；独立向量数据库在个人语料规模是过度设计；长上下文和 RAG 是互补的——我们的判断是持续运行的 Agent 按轮累积 token，检索必须做在查询时点。落地按 P1-P6：先分块解决上下文爆炸（零依赖），再混合检索配 golden set 回归，再逐模式接入。每一步都有市场实践背书，也有明确的验收判据。"

---

## 附：调研来源

- 框架格局：ayautomate.com《10 Best RAG Frameworks 2026》、olostep.com《Best Open Source RAG Frameworks 2026》、aiopsschool.com 对比、博客园《企业级知识库 RAG 技术调研（2026版）》
- 生产实践：howworks.ai《How AI Apps Are Built》、algoroq.io AI Engineering Guide（Notion/Cursor 细节）、genai-pro.com（Cursor Merkle 树/隐私脱敏）、deepseek.csdn.net Cursor RAG 调研
- 趋势：ragbible.com《State of RAG, mid-2026》（Claude Code 移除 embedding、GraphRAG 数据、agentic 架构对比）、icmd.app《The RAG Backlash》、CSDN《长上下文 vs RAG 真实权衡（2026 生产视角）》（500K 阈值、context rot）、dev.to 生产指南（hybrid 默认、chunking 陷阱、评估方法）
