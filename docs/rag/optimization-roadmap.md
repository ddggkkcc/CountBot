# CountBot RAG 架构优化路线图与选型决策报告

> 分支基线：`pr/rag-crag-refusal` @ 235c6c0（M1 分块 BM25 + CRAG 纠偏路由已合入）
> 评测基线：rag-bench 60 题（25 单文档 / 15 跨文档 / 10 针题 / 10 负样本），CRAG 结果 2026-09-03
> 本文定位：M2 之后的优化总纲（战略 + 选型论证）。所有选型基于**本仓库真实代码与真实评测数字**，不基于模板假设。
>
> **→ 开发落地请读 `docs/rag/implementation-plan.md`**（正式开发规格：文件路径 / 函数签名 / 数据结构 / 配置项 / 验收门禁 / 测试规格，含 P0 阻塞项 `chat_completion` API 断裂的修复）。

---

## 0. 结论先行（TL;DR）

**一句话：你的检索层缺的是"语义另一半"（向量 + RRF 混合），编排层已经超前（CRAG 纠偏路由业界主流都还没做到这个完成度）；下一步最高 ROI 不是加新架构，而是把 M2 混合检索做掉、把检索层指标基线补上，让 CRAG 的 grader 有更好的原料可判。**

| 阶段 | 周期 | 核心动作 | 预期收益（验收门禁） |
|------|------|---------|---------------------|
| **Phase 1** | ~1 周 | ① 检索层指标基线（Hit@6 / MRR）；② M2 混合检索（BM25 + BGE-M3）；③ 误拒答归因修复 | 召回缺口 4→0（2026-09-05 已达成）；正样本误拒答 4/50 → ≤1/50；引用率 72% → ≥85%（精排 Hit@6 提升归 Phase 2 rerank，见 §4） |
| **Phase 2** | 2~3 周 | ① bge-reranker-v2-m3 重排（top50→top6）；② grader 职责收缩（只判 grade 不做过滤）；③ 延迟预算与缓存 | MRR +20%（相对）；p95 ≤ 3s；单题 LLM 调用 2.2 次 → ≤1.5 次 |
| **Phase 3** | 按需 | ① 跨文档总结路由（查询分解 + map-reduce）；② M3 语义记忆；③ （明确不做：GraphRAG / LangGraph） | cross_doc 全源命中率 ≥0.5（当前 0.13） |

**同样重要的"不做什么"：**

- **不引入 Qdrant / Milvus** —— 全库只有 1,423 个块，numpy 暴力余弦 <5ms，引向量数据库是纯运维负债。
- **不上 GraphRAG** —— 30 篇文档的 wiki 没有实体网络可挖，图谱构建/维护成本远超收益（详见 §3.6）。
- **不引入 LangGraph** —— CountBot 自己就是 Agent 框架，`tool.py` 里的路由逻辑就是编排层，再套一层 LangGraph 是功能重复。
- **不做独立的"分级意图路由"组件** —— CRAG 的 grade→rewrite→refuse 已经是路由（纠正式）；单点/针题走同一条 hybrid+rerank 路径，通用多级分类器每题多一次 LLM 调用却零增益。唯一要做的那一级（single vs aggregate）已落在 Phase 3 的 map-reduce 前置判断里（详见 §3.7）。
- **不做 Contextual Retrieval（暂缓）** —— HeadingChunker 已带标题路径上下文，30 篇文档规模下该技术收益边际（详见 §3.5）。
- **不换 embedding 追新** —— Qwen3-Embedding-8B 分数更高但需要 GPU 自托管，BGE-M3 经 API 调用零运维，C-MTEB 中文检索已是第一梯队。

---

## 1. 现状基线（真实代码 + 真实数字）

先对齐事实，避免在错误假设上做规划。**本项目的实际形态与常见"512 固定切块 + 纯向量"模板完全不同**：

| 层 | 现状（已实现） | 证据 |
|----|--------------|------|
| 分块层 | HeadingChunker：标题节切分 + 超长节段落二次切分（max 1200 字符 ≈ 800 est tokens，overlap 120），代码围栏/表格不可分割，块携带 `slug#section` 溯源 | `backend/modules/rag/chunker.py` |
| 检索层 | **纯 BM25**（jieba 分词、标题×3 / 标签×2 加权、倒排索引），无向量通道 | `stores/bm25_store.py`，复用 `wiki/index.py` |
| 编排层 | **CRAG 纠偏路由**：LLM grader 判 all/partial/none → partial 过滤块、none 则重写查询一次再检索再判 → 仍 none 则显式拒答；全程降级安全（无 provider/解析失败回退直出） | `wiki/tool.py` `_rag_ask` 系列 |
| 评测 | 60 题四类（single_doc 25 / cross_doc 15 / needle 10 / negative 10），LLM judge（deepseek-v4-flash） | `rag-bench/questions.jsonl`、`results/crag-detail.json` |

**关键基线数字：**

- 注入 token：12,334 → 910 est tokens（**−92.6%**，G1 门禁 ≥70%）
- 负样本拒答：**10/10**（CRAG 上线前为 0/10，全部幻觉作答）
- 正样本正常回答：**46/50**（4 题误拒答）
- 带块级引用回答：**36/50 = 72%**
- G1 诚实回退记录：S3-07 针题 miss；S1-16/18 **语义变体题跌出 top10**；S2 跨文档全源命中率 0.20 → **0.13**
- 单题平均 LLM 调用：134/60 ≈ **2.2 次**（grader + 生成，含重写路径）

> 解读：这套系统的**编排层已达到生产级纠偏能力**（拒答率 100% 是很多生产 RAG 都做不到的），当前瓶颈**全部集中在检索层**——BM25 单通道召回不完整，导致 grader 拿到的候选池质量不足，既造成误拒答（4 题），也压低了引用率。

---

## 2. 根因剖析与方案映射

对模板中的五类痛点逐一剖析。**诚实声明：其中两类在当前项目中并不存在或形态不同**，我按真实形态映射。

### 痛点 1：专有名词/术语搜不准 → 【真实形态相反：语义变体才是漏洞】

- **技术瓶颈定位：检索层（召回通道单一）**。
- 本项目语料是 CountBot 文档（Markdown，大量 `COUNTBOT_RAG_CHUNKS`、`AgentLoop` 这类精确标识符）。**BM25 恰恰是精确标识符的强项**（字面匹配），"错误代码搜不准"在这个语料上不是主要矛盾。
- 真正的失败模式是**反向的**：G1 记录的 S1-16/18 语义变体题（用户用不同措辞问同一概念）跌出 top10——BM25 无法跨越"用词不同、语义相同"的鸿沟。
- **业界成熟范式：Hybrid Search（BM25 + Dense + RRF 融合）**。这正是原里程碑 M2 的规划，未开始。
- 注意一个反直觉结论：**"调大 Top-K"解决不了这个问题**。S1-16/18 的失败是 BM25 打分排序失败，扩大 K 只是稀释信噪比，还会把更多无关块喂给 grader 增加误判面。

### 痛点 2：跨文档宏观总结残缺 → 【真实存在，最硬的骨头】

- **技术瓶颈定位：检索层（召回覆盖）+ 编排层（无聚合策略），双重缺失**。
- 证据：S2 跨文档全源命中率 0.13——15 题 cross_doc 平均只召回约 13% 的应引用文档。"总结所有产品线风险"类问题需要 5+ 个来源块，top-6 单次检索在结构上就覆盖不了。
- **业界成熟范式**：查询分解（Query Decomposition）+ map-reduce 聚合——把"总结所有 X"分解为按文档/主题的子查询，分别检索生成，再归并。属于 Agentic 编排增强，不是检索组件问题。
- 定位到 **Phase 3**：它依赖 Phase 1/2 先把单点检索质量拉满（子查询的召回率是乘法关系，单点 0.7 的召回率做 5 路子查询的联合覆盖率会惨不忍睹）。

### 痛点 3：复杂 PDF/表格数值错位 → 【当前不适用，诚实标注】

- 本项目语料为纯 Markdown wiki，**无 PDF、无扫描件**。MinerU / 版面解析 / OCR 这条线当前是零收益投入。
- **How I would implement it（如果未来语料接入 PDF）**：MinerU（开源、中文 PDF 版面解析成熟）做解析层，表格抽为独立 chunk 类型打 `type: "table"` 标签交给 BM25 精确匹配（表格做 embedding 效果极差——数值列的语义向量没有区分度，这是 2026 年生产共识），跨页表格在解析层合并后再切块。**先有语料再上组件**，这条优先级永远排在实际需求之后。

### 痛点 4：统计类提问幻觉 → 【已被 CRAG 大幅缓解，剩余风险在语料形态】

- CRAG 拒答机制已把"语料里没有就硬编"压到 10/10 拒答。剩余风险：**语料中的数字被正确召回但被模型算错**（如"上季度总投诉量"需要对多块数值做聚合运算）。
- 当前语料无此类结构化数值数据，此风险**暂无暴露面**。若未来出现，正确解法不是更强的检索，而是**结构化查询通道**（数据进 SQLite，Agent 路由到 SQL 工具），属于 Phase 3 之外的独立决策点。

### 痛点 5：延迟与 Token 成本 → 【当前反而是优势项，但 Phase 2 有新增预算】

- 注入 token 已降 92.6%，这是最大头的成本。新增成本是 grader（单次 200~400 tokens，单题均 2.2 次 LLM 调用）。
- Phase 2 引入重排后有新预算：rerank 50 候选 ≈ 200~500ms（API 模式）。需要显式管理，见 §4 Phase 2 验收指标。

---

## 3. 技术选型与决策论证（核心）

### 3.1 向量通道：BGE-M3（经 OpenAI 兼容 API 调用）

**选型推荐**：BGE-M3（568M，1024 维，MIT 许可），通过 OpenAI 兼容的 `/v1/embeddings` 端点调用（SiliconFlow 等托管 BGE-M3 的国内 API，或自托管 vLLM/TEI）。落到原 M2 规划的 `embeddings.py`，保持规划中的 Noop 降级设计。

**决策依据（Why this）：**

| 维度 | 论证 |
|------|------|
| 中文检索质量 | C-MTEB 中文检索第一梯队；LangChain/LlamaIndex 2026 生产部署遥测中 BGE-M3 是使用量最大的开源 embedding |
| 工程成本 | 经 API 调用 = 零本地依赖（torch/sentence-transformers 都不用装），符合本模块"零第三方依赖"哲学（M1 就是这么做的）；embedding 只在索引期和查询期各跑一次，30 篇文档的量级 API 成本可忽略 |
| 架构兼容 | 8K 上下文足够覆盖 1200 字符的块；MIT 许可可自托管可商用，不锁死 |
| 降级安全 | 规划中的 Noop 降级路径不变：embedding 源不可用 → 回落纯 BM25，检索不中断 |

**Why not others：**

- **Qwen3-Embedding-8B**（MTEB 多语言第一，~75 分）：精度上限更高，但 8B 参数需 GPU 自托管（A10 级别），为 1,423 个块的索引维护一张 GPU 是典型的"benchmark 驱动选型"。**0.6B 版是合理备选**，但 BGE-M3 的生产验证更充分。
- **OpenAI text-embedding-3-small**：中文能力中等（MTEB ~62），且引入海外 API 依赖——本项目 grader 已在用国内 provider（deepseek），没必要混两套网络路径与计费。
- **本地 sentence-transformers**：需要引入 torch 全家桶（数 GB 依赖），对一个可选功能（flag 控制关闭即回退）来说依赖面太重。

### 3.2 向量存储：numpy 暴力检索（复用现有持久化模式）

**选型推荐**：`stores/vector_store.py` 用 numpy 矩阵存 1024 维向量 + 暴力余弦相似度，持久化并入现有 `chunk_index.json` 模式（或独立 `vector_index.npz`），与 BM25 的 slug registry 共享同一套增量同步。

**决策依据（Why this）—— 一道算术题：**

- 全库 1,423 块 × 1024 维 × float32 ≈ **5.6 MiB**。暴力检索 1,423 个向量的余弦相似度在现代 CPU 上 **<5ms**。HNSW/IVF 等 ANN 索引的加速意义在于**百万级以上**向量。
- 零新依赖、零新进程、零新运维面，与 `ChunkedBM25Index` 同构的 API 形态，代码量预计 <150 行。

**Why not Qdrant / Milvus（强烈不建议）：**

| 维度 | Qdrant/Milvus | numpy |
|------|--------------|-------|
| 部署 | 新增常驻服务（Docker/网络/健康检查） | 无 |
| 数据一致性 | 与 chunk_index.json 双写同步问题 | 同一持久化路径 |
| 延迟（1.4K 向量） | 网络 RPC 反而更慢 | <5ms 内存计算 |
| 迁移成本 | 引入客户端 SDK 依赖 | numpy 已是事实标准依赖 |

**唯一需要提前埋的钩子**：`vector_store.py` 的接口按"可替换 ANN 后端"设计（`search(query_vec, top_k) -> [(chunk_id, score)]`）。语料涨到 10 万块量级再换 sqlite-vec 或 Qdrant，届时只换实现不换接口。**为未来留接口、不为未来付成本。**

### 3.3 融合策略：RRF（Reciprocal Rank Fusion）

**选型推荐**：`retriever.py` 实现 `HybridRRF`，公式 `score(d) = Σ 1/(k + rank_i(d))`，k=60。

**Why RRF，why not 加权分数和：**

- BM25 分数与余弦相似度**量纲和分布完全不同**，加权融合需要 per-corpus 的归一化和权重调参（初始建议 0.7:0.3，需 A/B 微调）；换语料后静默失效。
- RRF 只依赖排名不依赖分数，**一个参数（k）、零校准、语料无关**——这是它成为业界默认的原因。生产实践共识：只有在你有评测 harness 持续盯着的情况下，才值得尝试调离 RRF。
- 对本项目额外的好处：RRF 输出仍是排名，与现有 `search_chunks` 返回结构兼容，grader/注入逻辑零改动。

### 3.4 重排器：bge-reranker-v2-m3（API 优先），并与现有 grader 重新分工

**选型推荐**：Phase 2 引入 bge-reranker-v2-m3（568M cross-encoder），经 API 调用；召回 top-50 → rerank → top-6 进生成。**同时收缩 grader 职责：只判 all/partial/none（拒答决策），不再做块过滤（交给 reranker 的精排）。**

**决策依据（Why this）：**

- **重排是单点 ROI 最高的检索升级**（业界基准：hybrid+RRF Recall@5 ≈0.695 → +cross-encoder ≈0.816，相对提升 17%+）。Bi-encoder（embedding）Query 和文档独立编码，快但粗；cross-encoder 两者联合注意力，准但慢——所以只对 top-50 跑。
- **与现有 grader 的关系是本节关键判断**：你的 CRAG grader（LLM 调用，200~400 tokens）事实上承担了一部分"重排"职能（partial 时过滤块），但它是为**拒答决策**设计的。引入 reranker 后形成正确分工：**reranker 管精排（批量推理，50 候选总延迟约 200~500ms，无 token 成本），grader 管拒答（LLM 判断力，管"全都不相关"这种语义判断）**。分工后 grader 输入的是精排后的 top-6，误判面更小，4 题误拒答有望收敛。
- API 调用（SiliconFlow 等托管 bge-reranker-v2-m3）保持零本地依赖；~1900ms 的自托管 CPU 延迟或 ONNX 优化问题都交给托管方。

**Why not others：**

- **Qwen3-Reranker-4B**：CMTEB-R 更高（75.94），但 4B 参数自托管成本/延迟都不匹配这个规模，且 reranker 质量差在这 1,423 块的语料上未必能体现。
- **Cohere rerank-v4**：海外 API，与现有 provider 体系不合。
- **继续用 LLM grader 兼职重排**：每次过滤都花 200~400 tokens 且延迟秒级，cross-encoder 是毫秒级/块（批量）。用 LLM 做机器能做的事，是 token 成本的隐形漏水点。

### 3.5 Contextual Retrieval（Anthropic 范式）：明确暂缓

**结论：暂缓，写清楚为什么**。Anthropic 的做法是索引期为每个块 LLM 生成一段"该块在全文中的位置说明"前置到块内容，解决"块脱离上下文后语义模糊"问题，官方数字是检索失败率降 35%~49%（配合混合检索）。

**为什么这里收益边际：**

- 该技术的收益前提是**块本身缺乏上下文信号**（如固定长度切出来的裸文本段）。HeadingChunker 的每个块已携带 `文档标题 · 层级标题路径`（如 `部署 > Docker`）并在 BM25 侧做了 ×3 加权——这正是 contextual retrieval 要手工补的东西，你已经结构化地拥有了。
- 30 篇文档 × 索引期每块一次 LLM 调用，成本可控但**收益集中在长文档的中段模糊块**，当前 60 题评测里没有专门暴露这个问题。
- **触发条件（写下来，到点再上）**：M2 混合检索上线后，若 needle 类题目 Hit@6 仍不达标且失败案例集中在"块内容不含问题关键词"的，则对失败案例做 contextual enrichment 再评测。用数据触发，不用焦虑触发。

### 3.6 跨文档总结：查询分解 + Map-Reduce；GraphRAG 明确不做

**选型推荐（Phase 3）**：ask 路由前置一个轻量判断——识别"宏观总结/枚举类"问题（可复用 grader 的 LLM 调用，输出 `{"scope": "single|aggregate"}`）→ aggregate 则分解为子查询（按文档标题列表生成 per-doc 查询）→ 各自走检索+rerank → map-reduce 归并生成。

**Why not GraphRAG（明确拒绝的论证）：**

| 维度 | GraphRAG 需要的 | CountBot 语料实际 |
|------|----------------|------------------|
| 语料形态 | 实体密集（人物/机构/事件网络），关系即答案 | 技术文档，核心实体是"模块名/配置项"，关系就是标题层级 |
| 构建成本 | 索引期 LLM 抽实体+关系，全量重建成本高 | 30 篇文档抽不出有意义的实体网络 |
| 查询收益 | "全局性总结"问题（community summarization） | 该需求用查询分解 + map-reduce 已可覆盖，成本低一个数量级 |
| 维护 | 图谱随语料更新需重算 | 与现有 mtime 增量同步机制冲突 |

**业界现状佐证**：GraphRAG 在 2025-2026 的生产落地集中在情报分析、法律文书这类实体网络语料；技术文档问答的生产主流仍是 hybrid + rerank + agentic 路由。**用错场景的先进技术比用对场景的朴素技术更贵。**

### 3.7 分级意图路由（追问专项：做不做？）

**结论：不建独立的"分级意图路由"组件；但"单点 vs 聚合"这一个分叉要做，且已落在 Phase 3 第 1 项。**

先厘清两个易混概念：

| 路由形态 | 定义 | 本项目现状 |
|---------|------|-----------|
| **纠正式路由**（corrective） | 先检索 → 再判定 → 决定改/拒 | **已有**：CRAG 的 grade→rewrite→refuse |
| **前置式路由**（upfront，即"分级意图路由"） | 先分类意图 → 再走对应策略链 | 未做 |

业界流行的"分级意图路由"是前置式多级分类器（LangChain/LlamaIndex 的 query routing / adaptive RAG），典型分级：Tier 0 闲聊/FAQ 直答 → Tier 1 单点事实单次检索 → Tier 2 跨文档聚合多检索 + map-reduce → Tier 3 结构化/工具类走 SQL/API。

**为什么当前不建（四点论证）：**

1. **你已经有路由了**——CRAG 就是路由，只是"纠正式"。再叠一层前置分类器 = 每题多一次 LLM 调用 + 两套路由逻辑并存，是评审时的解释负担。
2. **多级里有两级对检索策略零差别**——single_doc（25 题）与 needle（10 题）走同一条 hybrid+rerank 路径，前置分类对它们无增益，纯加延迟。
3. **负样本无法前置可靠分类**——评测已证：negatives 的 top-1 BM25 分与 positives 重叠 7/10，任何前置分数门控都挡不住，grader 事后判才是对的（这正是 CRAG 存在的理由）。"Tier 前置拒答"不可行。
4. **Tier 0 在本语料里不存在**——wiki 问答没有闲聊/FAQ 场景，加这一级是空转。

**唯一要做的那一级：`single vs aggregate`。** 它就是 Phase 3 第 1 项（map-reduce）的前置判断，且**不必单独建分类器**：复用 grader 一次调用输出 `{"scope": "single|aggregate"}`（§3.6 已写），或更省地用检索结果信号（候选块来源文档数 > 阈值即视为聚合类）即可。

> 注：若你问的"分级意图路由"指**跨工具路由**（wiki / memory / news / 外部工具之间按意图分发），那是 CountBot `ToolRegistry` 已承担的职责，不在本文检索链范围，也无需在本路线图内重建。

### 3.8 编排框架：不引入 LangGraph，理由是身份问题

CountBot **本身就是一个 Agent 框架**（AgentLoop / ToolRegistry 是它的核心资产）。CRAG 的 grade→rewrite→refuse 状态机就是用 `tool.py` 里的普通 async 控制流写的，138 个测试全绿。引入 LangGraph 意味着：在 Agent 框架里嵌另一个 Agent 编排框架，双份状态管理、双份依赖、上游 review 时解释成本翻倍。**这里"轻"就是竞争力——向上游提案 PR 时，"零新依赖实现了纠偏路由"比"我们用了 LangGraph"是强得多的工程叙事。**

### 3.9 评估体系：把 rag-bench 从"端到端 LLM judge"补成"分层指标"

当前 60 题评测是端到端的（LLM judge 答案质量），**检索层的中间指标缺失**——这意味着你无法回答"这 4 题误拒答是检索没召回，还是 grader 误判"。这是 Phase 1 第①项的原因：

- **Hit@K / MRR**：`questions.jsonl` 每题已有 `source_pages`，检索层指标**不需要 LLM**，纯确定性计算，加进 `run_crag_eval.py` 即可（预计半天工作量）。
- **Faithfulness / Context Precision / Recall**（RAGAS 定义）：LLM judge 已有基础设施，扩展 prompt 即可；对应原 M2 验收标准（precision ≥0.70 / recall ≥0.85 / faithfulness ≥0.85）。
- **分层归因表**：每题记录"检索命中? → rerank 后命中? → grader 判定 → 是否引用"，误拒答归因从猜测变成查表。

---

## 4. 演进实施阶段与优先级

> 排序原则：ROI = (指标收益 × 暴露面) ÷ (工程成本 + 运维成本)。每阶段结束跑同一份 60 题 bench，数字不达标不进下一阶段——这是 M0 就立下的"门禁不过即回退"纪律，继续沿用。

### Phase 1（~1 周）：指标基线 + M2 混合检索（最高 ROI）

| # | 改动点 | 具体内容 | 量级 |
|---|--------|---------|------|
| 1 | 检索指标基线 | `run_crag_eval.py` 增加 Hit@6 / MRR / 全源命中率输出（用 `source_pages`，零 LLM 成本） | ~半天 |
| 2 | `embeddings.py` | OpenAI 兼容 embedding 客户端 + Noop 降级（沿用原 M2 规划设计） | ~1 天 |
| 3 | `stores/vector_store.py` | numpy 暴力余弦 + 持久化 + 与 BM25 共享增量同步（mtime + content_hash 双校验） | ~1 天 |
| 4 | `retriever.py` HybridRRF | BM25 top-50 + Dense top-50 → RRF(k=60) → top-6；flag `COUNTBOT_RAG_HYBRID=1`，关闭即回纯 BM25 | ~1 天 |
| 5 | 误拒答归因 | 用新指标对 4 题误拒答做归因（检索 miss vs grader 误判），分别修复 | ~半天 |

**验收门禁（2026-09-05 修订：拆成两层——召回层 Phase 1 判，精排层 Phase 2 rerank 判。修订原因见 work-log §2.3 存档，核心两条：① 原 "needle+cross 25 题池 +15pp" 数学上不可能（25 题每题 4pp，全对才 +12pp），且 needle 三类通道已全对、无提升空间；② 实测 RRF 融合只解决召回面、不解决 top-6 精排）：**

- **召回层（Phase 1 验收，2026-09-05 BGE-M3 实测已达成 ✅）**：向量通道把 top-50 相关覆盖缺口从 4 题（BM25 结构性盲区：语义问法零词面重叠）压到 0。判据：`report_layer_gates.py`（49 正题去重口径）。召回缺口的定义 = 该通道 top-50 候选内无任何 relevant 块——这是"通道找不到答案"的唯一判据，与 top-6 注入窗口无关。
- **精排层（Phase 2 rerank 验收）**：hit@6 的提升不要求 RRF 阶段实现——RRF/融合是召回面工具，top-50→top-6 精排归 reranker。目标 = dense/rerank 后 hit@6 高于 G1 基线（needle 已饱和不计入）。原 "+15pp" 表述作废。
- 正样本误拒答 ≤1/50；引用率 ≥85%
- **停级条款（重定义）**：召回缺口 4→0 已达成，证明"该语料用得上向量"（BM25 存在 4 个词面无法覆盖的真实盲区，已逐一核实是语义问法而非标注问题）；停级条款改为只在此处触发：dense 通道 top-50 覆盖缺口仍 ≥ G1（即向量连召回面都没补上）——当前数据不触发。
- 全量测试绿；无 embedding 配置时行为与现状完全一致（降级安全）

### Phase 2（2~3 周）：重排 + grader 分工重构

| # | 改动点 | 具体内容 |
|---|--------|---------|
| 1 | reranker 接入 | bge-reranker-v2-m3 API：RRF 融合 top-50 → rerank → top-6；同样 flag 门控 + 失败回落 RRF 结果 |
| 2 | grader 职责收缩 + 置信门控 | 移除 partial 过滤（交给 reranker），grader 只判 all/none；rerank top-1 高分时跳过 grader 直出生成（降调用次数），仅低置信才走 none/rewrite 判定 |
| 3 | 查询缓存 | grader/rerank 结果按 (query, chunk_set_hash) 做 LRU 缓存（生产实践：查询重复度比想象高） |
| 4 | 延迟预算表 | 记录 p50/p95 分段延迟（检索 / rerank / grader / 生成），进 bench 报告 |

**验收门禁：**

- MRR 相对提升 ≥20%；rerank 后 top-6 的 Hit@6 ≥90%（正样本）
- 单题 LLM 调用均值 2.2 → ≤1.5 次；grader prompt 收缩 ≥40%
- p95 端到端 ≤3s（rerank API 预算 ~500ms 内）
- 60 题全量回归：拒答 10/10 不回退，正样本 ≥48/50

### Phase 3（按需推进，长尾覆盖）

| # | 改动点 | 触发条件 |
|---|--------|---------|
| 1 | 跨文档 map-reduce 路由（查询分解） | Phase 2 后 cross_doc 全源命中率仍 <0.5 |
| 2 | Contextual Retrieval（对失败案例） | needle 类 Hit@6 不达标且失败集中在"块内无查询关键词" |
| 3 | M3 语义记忆（memory 嵌入 + SemanticRecency） | 主线需求，与 wiki RAG 独立推进 |
| 4 | M4 时序聚合（news_pool） | 同上，按原里程碑节奏 |
| 5 | 结构化数据通道（SQLite + SQL 工具路由） | 语料出现数值聚合需求时（当前无暴露面） |
| — | **GraphRAG / LangGraph / 向量数据库** | **明确不做**（论证见 §3.2/§3.6/§3.7） |

---

## 5. 架构拓扑（端到端数据流）

```mermaid
flowchart TB
    subgraph OFFLINE["离线索引构建（写路径）"]
        MD["concepts/*.md<br/>(mtime 变更检测)"] --> HC["HeadingChunker<br/>标题切分 + 超长节二次切分<br/>(代码围栏/表格不可分割)"]
        HC --> CHUNK["Chunk 池<br/>1,423 块 · slug#section 溯源"]
        CHUNK --> BM25I["BM25 倒排索引<br/>(jieba · 标题×3 · 标签×2)"]
        CHUNK --> EMB["embeddings.py<br/>(BGE-M3 API · Noop 降级)"]
        EMB --> VEC[("vector_store<br/>numpy 1024d<br/>~5.8MB")]
        BM25I --> IDX[("chunk_index.json<br/>mtime+content_hash 增量同步")]
        VEC --> IDX
    end

    subgraph ONLINE["在线 ask 路由（读路径 · tool.py）"]
        Q["用户问题"] --> HYB["HybridRRF<br/>BM25 top-50 ⊕ Dense top-50<br/>RRF(k=60)"]
        BM25I -.-> HYB
        VEC -.-> HYB
        HYB --> RR["bge-reranker-v2-m3<br/>top-50 → top-6<br/>(失败回落 RRF 排序)"]
        RR --> GR{"LLM Grader<br/>(收缩后: all / none)"}
        GR -- "all" --> GEN["生成 · 块级引用注入"]
        GR -- "none" --> RW["查询重写 ×1"]
        RW --> HYB2["再检索 + 再判"] --> GR2{"仍 none?"}
        GR2 -- "是" --> REFUSE["显式拒答<br/>(不幻觉)"]
        GR2 -- "否" --> GEN
        GR -.-> "|降级: 无 provider/解析失败|" --> GEN
    end
```

**读路径的三层防御设计（这是本架构的生产级特征，建议在 PR 叙事中保留）：**

1. **检索层双通道互补**：BM25 管精确标识符（`COUNTBOT_RAG_CHUNKS` 类），Dense 管语义变体（S1-16/18 类），RRF 融合互不牺牲；
2. **精排层无状态可回退**：reranker 失败 → 回落 RRF 排序，质量降级不中断；
3. **编排层降级链**：grader 失败 → 回退直出（M1 行为）；全链路任何一环无配置时，系统行为逐级回落到已验证的上一形态。

---

## 6. 风险与对策

| 风险 | 概率 | 对策 |
|------|------|------|
| BGE-M3 API 不可用/延迟抖动 | 中 | Noop 降级回落纯 BM25（已在设计中）；embedding 结果持久化，查询期只需 embed 问题本身 |
| rerank API 延迟超标 | 中 | LRU 缓存 + 候选数上限 50 + 回落 RRF |
| 混合检索对 S1 无提升（停级条款触发） | 低 | 诚实记录为有效结论，符合项目既有纪律 |
| 向量与 BM25 索引漂移（单边更新） | 低 | 共享 slug registry 与 mtime 校验，同一 sync 事务内双写 |
| grader 收缩后拒答率回退 | 低 | Phase 2 验收门禁显式盯 10/10 拒答不回退 |

---

## 附：推荐阅读顺序

1. **§0 结论先行** —— 3 分钟掌握全部决策
2. **§1 现状基线** —— 确认数字口径（所有后续验收都以此为对照）
3. **§3.1~3.4** —— 四个核心选型的完整论证（M2 落地前必读）
4. **§4 Phase 1** —— 下周开工清单
5. **§3.5~3.8** —— "暂缓/不做"的边界论证（防止 scope creep）
6. **§5 架构图** —— PR/面试叙事时的讲解底稿
