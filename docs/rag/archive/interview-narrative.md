> ⚠️ **已归档（2026-08-30）**：本文为个人资产（面试叙事弹药库），仅供本人回溯，不再维护。
> 文档入口（职责速查）见 `docs/rag/README.md`；现行体系：`work-log.md`（工程日志）、`optimization-roadmap.md`（战略）、`implementation-plan.md`（开发规格）、`pr-submission-handbook.md`（PR 手册）。

# CountBot × RAG：必要性、场景、落地路径与面试讲述框架

> 用途：把"要不要给 CountBot 加 RAG"这件事讲透，并作为面试中讲述 RAG 使用案例的弹药库。
> 一句话结论：**CountBot 不是"要不要加 RAG"，而是"把已有的 BM25 词法检索升级成语义+分块混合 RAG"。** 它已站在阶梯最底层（关键词召回），升级目标明确是 Hybrid（BM25+向量+RRF）。

---

## 一、CountBot 现状真相（代码级审计）

| 能力 | 文件 | 机制 | 本质 |
|------|------|------|------|
| Wiki 知识库 | `backend/modules/wiki/*` | jieba 分词 + BM25 倒排，文档级召回 | 词法全文检索 |
| Wiki `ask` | `backend/modules/wiki/tool.py:170` | BM25 top-3 **整篇文档** → 拼进 prompt → LLM 生成 | 检索增强生成（无向量） |
| 记忆 | `backend/modules/agent/memory.py` | 行式 `MEMORY.md` + 子串 OR/AND 搜索 | 对话记忆，非语料检索 |

关键证据：
- `tool.py:175` `results = self._service.search(question, top_k=3)`，召回的是**整篇 markdown**（`get_document` 返回全量 `content`），再整体塞给 `provider.chat_completion`。
- `index.py` 是纯 BM25：中文 jieba、英文连字符分词、停用词黑名单、标题×3/标签×2 加权。**无 embedding、无向量库、无分块、无重排**。
- 索引文件 `bm25_index.json` 持久化 + LRU 缓存，`force_sync` / `_check_and_index_new_files` 用 `mtime` 做增量。

> 项目里已有一份 `archive/rag-enhancement-plan.md`（分析阶段，未写代码，已归档），结论是"升级为分块 + BM25/向量混合 + RRF"，并已拆好 Phase 0–3；正式设计见 `architecture.md`，测试方案见 `test-plan.md`，里程碑见 `milestones.md`。

**现有短板（面试可如实说）：**
1. 整篇召回：3 篇 80KB 面经 ≈ 240KB 原文塞进上下文，超窗口且噪声大。
2. 纯词法：无语义匹配，表述不一致即 miss（"面试官爱问哪些并发题" vs 文档"高并发场景"）。
3. 无引用 / 无重排：回答不可溯源，结果未精排。
4. 无分块：无法做片段级精准定位。

---

## 二、有没有必要？适不适合？（分场景判定）

### 三类场景判定
| 场景 | 判断 | 理由 |
|------|------|------|
| 长文档语料（如 `interview-digest/` ~80KB 面经） | **强烈建议上** | 整篇召回打爆上下文、噪声大、精度低；分块+语义召回收益明确 |
| 大量短文档 + 语义/同义查询 | **建议上** | BM25 词法无法命中 paraphrase，embedding 能补 |
| 几十篇带好标题/标签的短文档 + 关键词查询 | **不必上** | BM25 召回率已够，上向量是过度设计 |

**CountBot 落在第一类 → 值得做。** 即便不上向量，只做"分块 + BM25 分块级召回"也能立刻缓解上下文爆炸。

### 通用"要不要 RAG"判断清单（面试通用）
- 知识**不在**训练集内（私有/最新/长尾）→ RAG。
- 知识**频繁更新**（每月改）→ RAG 优于微调（更新只要刷索引）。
- 需要**可溯源引用**（合规/审计）→ RAG。
- 单文档 < 200K token 且需深度推理 → 直接长上下文，不必 RAG。
- 要的是**风格/格式/专有词汇内化** → 微调（可与 RAG 并用）。
- 几十篇短文档 + 关键词查询 → 关键词检索（BM25）足矣，别上向量。

---

## 三、什么场景用 RAG（模式 × 场景映射）

见决策阶梯图。从底到顶逐级应对不同失败模式：

1. **Naive RAG**（分块+向量+top-K）→ 简单 FAQ、干净事实查询、MVP。
2. **Hybrid 检索**（BM25 + 密集向量 + RRF 融合）→ 术语/代号/专名/混合语料。**单路最高 ROI 升级**，几乎所有向量库已原生支持。
3. **Retrieve-then-Rerank**（cross-encoder 重排 top50→top5）→ 精度敏感，top 结果"接近但错"。
4. **Query Transform**（HyDE / 多查询 / 问题分解）→ 模糊或复合问题、用户词汇与文档错位。
5. **Corrective / Self-RAG** → 高利害准确性，检索不足或低置信则拒答/重试。
6. **GraphRAG**（知识图谱增强）→ 跨文档关系合成（金融/法务/代码依赖多跳）。
7. **Agentic RAG** → 复杂多跳研究，Agent 自主决定何时检索、检索什么、是否足够。

> 进阶：ColPali / 视觉 late-interaction 模型绕过 OCR 直接处理 PDF/扫描件；Late Chunking（先整体 embedding 再切分，保留上下文）。

---

## 四、怎样加入 RAG（CountBot 具体路径，引用真实代码）

升级而非重写，复用现有 `workspace/wiki` 语料与 LiteLLM provider。

```
workspace/wiki/concepts/*.md (或 interview-digest/)
        │
        ▼
   chunker.py       按 Markdown 标题层级切分 + overlap，每片带 heading path
        ├─► index.py(改造)  BM25 分块索引 ─┐
        └─► embeddings.py → vector_store.py (faiss/numpy) ─┤ 并行检索
                                            retriever.py  RRF 融合
                                                │
                                        WikiTool.ask(改造) 片段+引用 → LLM
```

新增/改动文件（对应 `archive/rag-enhancement-plan.md`）：
- `chunker.py` 新增：按标题层级切分 + overlap。
- `embeddings.py` 新增：EmbeddingProvider 接口，复用 LiteLLM。
- `vector_store.py` 新增：轻量向量存储（numpy / faiss-cpu）。
- `retriever.py` 新增：BM25 + 向量并行 + RRF。
- `index.py` 改造：文档级 → 分块级。
- `tool.py` 改造：`ask` 取片段 + 引用（`slug#标题`），限 token。
- `service.py` 改造：`force_sync` 增量时一并分块 + embed。
- 落盘：`workspace/wiki/vectors.json` + 分块映射表。

**分阶段（evidence-driven，先小后大）：**
- **Phase 1（最小可用，零新依赖）**：分块 + BM25 分块级召回。先把上下文从"整篇 80KB"降到"若干 ~500 token 片段"。风险低、立刻见效。
- **Phase 2（混合检索）**：向量嵌入 + RRF 融合。语义/同义查询命中率提升。风险中（嵌入质量、增量一致性、云端成本）。
- **Phase 3（精排+引用+开关）**：可选 cross-encoder reranker；`ask` 输出带 `slug#标题` 引用；Wiki 面板加"混合/语义/词法"切换。风险低。

**Chunk schema 草案**：`{chunk_id, doc_id, heading_path[], content, tokens, mtime}`。

**技术选型（Phase 0 待定）**：
- 嵌入：`text-embedding-3-small`（起步推荐，零部署）/ `bge-m3`·`m3e`（本地、中文优化、离线）/ 可切换。
- 向量库：纯 numpy 余弦（中小语料）/ `faiss-cpu`（推荐默认）/ Chroma·Qdrant（规模大/多租户）。
- 重排（Phase 3）：`bge-reranker` 等 cross-encoder。

**开放风险**（面试可诚实提及）：语料接入范围（面经在 `interview-digest/`，Wiki 只索引 `workspace/wiki/concepts/`）、中文 embedding 质量、增量仅靠 `mtime` 不够（建议 `mtime + content_hash` 双校验）、与已有 `ima-knowledge-base` 外部语义检索的关系（本方案聚焦本地私有语料，IMA 作外部补充）。

---

## 五、当前成熟方案（2026 现状）

**编排框架**
- **LangChain**：生态最大，链/工具/索引抽象全，但易过度抽象。
- **LlamaIndex**：RAG 一等公民，`SemanticSplitterNodeParser` 自动语义切分、父子文档、丰富索引。
- **轻量自研**：CountBot 现状（直接 BM25 + LLM），适合不想引入重依赖的中小场景。

**向量数据库**
- **Qdrant / Weaviate / Milvus / Pinecone**：生产级，混合检索原生支持。
- **pgvector**：语料 < 500–1000 万向量时，复用现有 Postgres 即可。
- **Chroma**：轻量、开发友好。
- **numpy / faiss-cpu**：极小语料、零运维（CountBot 计划默认）。

**Embedding 模型**
- 云端：`text-embedding-3-large`（安全默认）。
- 开源多语 SOTA：`Qwen3-Embedding-8B`（MTEB ~70.58）；中文优先：`bge-m3` / `m3e`。

**Reranker**：Cohere Rerank v3、Voyage Rerank、开源 `bge-reranker-v2`。检索 top50 重排到 top5，质量提升 15–30%，成本 < 100 行代码。

**Chunking**：语义切分 > 固定长度；父子文档（命中返回父段）；Late Chunking（ColBERT 思路）；overlap 10–25%。

**Eval（必做，否则是 demo 不是系统）**：RAGAS / DeepEval / ARES。指标分开看：Faithfulness（是否基于上下文）、Answer Relevance、Context Precision、Context Recall。建 50–200 条人工标注 golden set，接入 CI。

**进阶模式**：GraphRAG（Microsoft，LazyGraphRAG 降低索引成本，适合多跳）、Agentic RAG（Agent 动态选检索策略）、Self-RAG（自反思拒答）、ColPali（视觉文档绕过 OCR）。

---

## 六、面试讲述框架（把 CountBot 当案例讲）

### 1 分钟叙事（可直接背）
"CountBot 是一个开源 AI Agent 框架。它 v0.9.0 已经有一个 Wiki 知识库，用 BM25 做关键词检索增强——用户输入问题，召回最相关的几篇文档整篇塞给 LLM 生成。这其实已经是 RAG 的雏形，但它是词法版：没有向量、没有分块、没有重排。我们面经语料单份就有 80KB，整篇召回会打爆上下文、噪声大。所以我主导设计了一套升级方案：先把文档按标题层级分块（Phase 1，立刻缓解上下文爆炸），再叠加向量嵌入做 BM25+密集向量混合检索、用 RRF 融合（Phase 2），最后加 cross-encoder 重排和可溯源引用（Phase 3）。选型上嵌入先用云端 text-embedding-3-small，中文优化可切 bge-m3，向量库用 faiss-cpu 零运维起步。整个过程是 evidence-driven：先上分块验证收益，再决定要不要上向量。"

### 高频追问 Q&A（准备 15 题）
1. **RAG 和微调怎么选？** → 知识新/私有无/E[需溯源]用 RAG；风格/格式内化用微调；可并用。
2. **为什么不直接用 LangChain/Chroma？** → 框架重、依赖多；CountBot 是轻量本地 Agent 中枢，BM25 已能跑，先最小改造（分块级 BM25）零新依赖见效，再按需加向量；避免为"未来可能"引入运维成本。
3. **为什么 Hybrid 比纯向量好？** → 密集向量弱在专名/代号/错误码（"OOM""ERR_429"），BM25 擅长精确匹配；RRF 融合两路，几乎每个基准都赢。
4. **RRF 是什么？和加权融合区别？** → 按排名倒数求和融合，不需训练权重、对分数量纲不敏感；加权需归一化且敏感。
5. **分块大小怎么定？** → 128–512 token，overlap 10–25%；按语义单元（段落/小节）而非字符数；父子文档保留上下文。
6. **为什么重排不是可选？** → 向量检索是粗筛，cross-encoder 按真实相关性精排，top50→top5 常是"demo 能用"和"用户敢信"的差距。
7. **增量索引怎么做？** → 现有用 mtime；但复制/恢复 mtime 可能不变，建议 `mtime + content_hash` 双校验。
8. **怎么评估 RAG 好坏？** → RAGAS 四维（faithfulness/answer relevance/context precision/context recall），建 golden set 接 CI。
9. **检索到了错误块会怎样？** → LLM 会自信补全→幻觉；所以用重排+faithfulness 检查，低置信回退"不知道"。
10. **GraphRAG 什么时候值得？** → 语料有明确实体关系、查询需多跳合成（"X 的所有依赖"）；否则过度设计。
11. **Agentic RAG 是什么？** → Agent 自主决定何时检索、检索什么、是否足够，适合复杂研究；代价是延迟与编排复杂度。
12. **中文 embedding 怎么选？** → 优先中文优化 bge-m3/m3e，或 Qwen3-Embedding 多语 SOTA；云端可用 text-embedding-3 系列。
13. **长上下文（200K）会取代 RAG 吗？** → 互补：RAG 在大规模语料里找对材料，长上下文对完整文档深度推理；最佳系统两者结合。
14. **HyDE 是什么、什么时候用？** → 先让 LLM 生成"理想答案"再 embed 去检索，缓解词汇错位，召回常 +10–15%；适合用户问法与众不同的场景。
15. **怎么控制成本？** → 嵌入 batch、rerank 只跑 top50、小嵌入模型+重排常胜过大嵌入模型单路、增量索引避免全量重算。

### 应对"你这不算真 RAG"类反问
"严格说当前是词法版 RAG（检索→生成闭环已有）。真·语义 RAG 的差距在无向量/分块/重排——这正是我设计的升级路径要补的。我强调的是**先量化痛点（整篇召回 240KB 上下文）、再 evidence-driven 逐级上**，而不是为了'上了 RAG'而上一套重框架。这也是 2026 年主流观点：简单基线（语义分块+混合+重排）实现得好，胜过糟糕实现的先进 pipeline。"

---

## 七、关键取舍与坑（诚实清单）
- **别为未来过度设计**：几十篇带标签短文档用 BM25 就够，上向量是浪费。
- **先做分块再上向量**：分块级 BM25 已解决最大痛点（上下文爆炸），且零新依赖。
- **评估先行**：没 eval 集就是猜，不是工程。
- **增量一致性**：mtime 不够，加 content_hash。
- **与现有能力的边界**：IMA 知识库是外部语义检索，CountBot 本地方案聚焦私有语料，避免重复。
- **数据质量决定上限**：RAG 回答质量差，46% 根因是文档本身不完备/不足。
