# CountBot RAG 增强方案（分析 + 实施计划）

> 状态：分析阶段，待评审。尚未写代码。
> 结论摘要：现有 Wiki 已是「检索→生成」雏形，应**升级为分块 + BM25/向量混合 + RRF**，而非从零新建。是否落地、用哪种嵌入，Phase 0 再定。

---

## 1. 必要性分析

### 1.1 现状审计（代码级）

CountBot 已有两层"知识/记忆"能力，但都不是严格意义上的向量 RAG：

| 模块 | 文件 | 机制 | 本质 |
|------|------|------|------|
| Wiki 知识库 | `backend/modules/wiki/*` | jieba 分词 + BM25 倒排，文档级召回 | 词法全文检索 |
| Wiki `ask` 动作 | `backend/modules/wiki/tool.py:170` | BM25 top-3 **整篇文档** → 拼进 prompt → LLM 生成 | 检索增强生成（无向量） |
| 记忆 | `backend/modules/agent/memory.py` | 行式 `MEMORY.md` + 子串 OR/AND 搜索 | 对话/个人记忆，非语料检索 |

关键证据：
- `tool.py:175` `results = self._service.search(question, top_k=3)`，召回的是**整篇 markdown**（`get_document` 返回全量 `content`），再整体塞给 `provider.chat_completion`。
- `index.py` 是纯 BM25：中文 jieba 分词、英文连字符分词、停用词黑名单、标题×3/标签×2 加权。**无任何 embedding、无向量库、无分块、无重排**。
- 索引文件 `bm25_index.json` 持久化 + LRU 缓存，`force_sync` / `_check_and_index_new_files` 用 `mtime` 做增量（`service.py`）。

**所以：你不是"要不要加 RAG"，而是"要不要把 BM25 整篇召回升级成语义+分块混合 RAG"。**

### 1.2 三类场景判定

| 场景 | 判断 | 理由 |
|------|------|------|
| 长文档语料（如 `interview-digest/` 的 ~80KB 面经） | **强烈建议上** | 整篇召回直接打爆上下文、噪声大、精度低；分块+语义召回收益明确 |
| 大量短文档 + 语义/同义查询（"面试官爱问哪些并发题" vs 文档"高并发场景"） | **建议上** | BM25 词法无法命中 paraphrase，embedding 能补 |
| 几十篇带好标题/标签的短文档 + 关键词查询 | **不必上** | BM25 召回率已够，上向量是过度设计 |

本项目落在第一类 → **值得做**。

### 1.3 现有方案的具体短板

1. **整篇召回**：3 篇 80KB 面经 ≈ 240KB 原文塞进上下文，远超窗口，且无关内容稀释注意力。
2. **纯词法**：无语义匹配，表述不一致即 miss。
3. **无引用 / 无重排**：回答不可溯源，结果未精排。
4. **无分块**：无法做片段级精准定位。

---

## 2. 设计目标与非目标

**目标**
- 混合检索：BM25（分块级）+ 向量，RRF 融合。
- 片段级召回 + 可溯源引用（`[slug#标题]`）。
- 增量索引，复用现有 `mtime` 机制，避免重算。
- 零新微服务，沿用 `workspace/wiki` 知识源与 LiteLLM provider 配置。
- 中文优先。

**非目标**
- 不引入独立向量数据库服务（除非规模需要）。
- 不替换 BM25（保留作为混合之一路）。
- 不做多模态 / 表格问答（v2 再议）。

---

## 3. 架构与模块拆分（对应现有代码）

```
workspace/wiki/concepts/*.md  (或扩展至 interview-digest/)
        │
        ▼
   chunker.py       按 Markdown 标题层级切分 + overlap，每片带 heading path
        │
        ├──────────► index.py (改造)   BM25 分块索引   ──┐
        │                                               │
        └──────────► embeddings.py → vector_store.py    ──┤ 并行检索
                   (复用 LiteLLM provider)  (faiss/numpy) │
                                                        ▼
                                               retriever.py  RRF 融合
                                                        │
                                                        ▼
                                               WikiTool.ask (改造) 片段上下文 + 引用 → LLM
```

新增 / 改动文件：
- `backend/modules/wiki/chunker.py` **新增**：分块逻辑。
- `backend/modules/wiki/embeddings.py` **新增**：EmbeddingProvider 接口，复用 LiteLLM。
- `backend/modules/wiki/vector_store.py` **新增**：轻量向量存储（numpy / faiss-cpu）。
- `backend/modules/wiki/retriever.py` **新增**：BM25 + 向量并行 + RRF。
- `backend/modules/wiki/index.py` **改造**：从"文档级"改为"分块级"索引。
- `backend/modules/wiki/tool.py` **改造**：`ask` 调用 retriever，取片段 + 引用，限 token。
- `backend/modules/wiki/service.py` **改造**：`force_sync` 增量时一并分块 + embed。
- 索引落盘：`workspace/wiki/vectors.json`（或 `.faiss`）+ 分块映射表。

**Chunk schema 草案**
```json
{
  "chunk_id": "slug#3",
  "doc_id": "slug",
  "heading_path": ["面试准备", "并发题"],
  "content": "...",
  "tokens": 480,
  "mtime": 1723000000
}
```

---

## 4. 分阶段实施计划

### Phase 0 · 评估与决策（当前阶段）
- 确定：嵌入来源（云端 API / 本地模型 / 可切换）、向量库（轻量 / Chroma）、语料范围（是否纳入 `interview-digest/`）。
- 产出：本方案评审通过。

### Phase 1 · 分块 + BM25 分块级召回（最小可用，不依赖向量）
- 改动：`chunker.py` + `index.py` 改为分块级 + `WikiTool.ask` 取片段。
- 验收：同一查询返回的上下文从"整篇 80KB"降为"若干 ~500 token 片段"；回答更聚焦。
- 收益：即使不上向量，也已解决最大痛点（上下文爆炸）。
- 风险：低。

### Phase 2 · 向量嵌入 + 混合检索
- 改动：`embeddings.py` + `vector_store.py` + `retriever.py`（RRF 融合）+ `service.py` 增量 embed。
- 验收：语义/同义查询命中率提升；混合 > 单路。
- 风险：中（嵌入质量、增量一致性、云端成本）。

### Phase 3 · 重排 + 引用 + 前端开关
- 改动：可选 cross-encoder reranker；`ask` 输出带 `slug#标题` 引用；Wiki 面板加"混合/语义/词法"切换与片段级结果展示。
- 验收：回答可溯源；面板可切检索模式。
- 风险：低。

---

## 5. 技术选型对比（供 Phase 0 决策）

### 5.1 嵌入模型
| 选项 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| 云端 `text-embedding-3-small` | 质量高、零部署、复用 LiteLLM | 出网、按量计费 | 起步推荐 |
| 本地 `bge-m3` / `m3e` (Ollama) | 数据不出本机、离线 | 需本地算力、首次拉模型 | 隐私/离线优先 |
| 可切换 | 灵活 | 配置复杂度略高 | 长期 |

### 5.2 向量库
| 选项 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| 纯 numpy 余弦 | 零依赖、文件落盘 | 规模 >5k 片变慢 | 语料中小 |
| `faiss-cpu` | 快、轻量 | 多一个依赖 | 推荐默认 |
| Chroma / Qdrant | 持久化、元数据过滤、可扩展 | 运维成本 | 规模大/多租户 |

### 5.3 重排（Phase 3 可选）
- `bge-reranker` 等 cross-encoder，对 RRF top-N 再精排，提升精度。

---

## 6. 风险与开放问题

1. **语料接入**：面经当前在 `interview-digest/`，Wiki 只索引 `workspace/wiki/concepts/`。需决定：把面经纳入 Wiki 索引范围，还是新建独立语料目录（建议前者，统一索引）。
2. **中文 embedding 质量**：本地模型优先选中文优化的 `bge-m3` / `m3e`。
3. **增量一致性**：仅靠 `mtime` 不够（内容改了 mtime 变，但复制/恢复可能不变）；建议 `mtime + content_hash` 双校验。
4. **成本**：云端 embedding 对大语料首次建索引有调用量，需 batch。
5. **与 IMA 知识库的关系**：`ima-knowledge-base` 已提供外部语义检索，避免功能重复——本方案聚焦**本地私有语料**（如面经），IMA 作为外部补充。
6. **长文档切分策略**：面经按题号/标题切分效果最好，需验证 `chunker` 对现有 md 结构的适配。

---

## 7. 下一步建议

1. 你确认 Phase 0 的三个决策点（嵌入来源 / 向量库 / 语料范围）。
2. 我可先落地 **Phase 1（分块 + BM25 分块级召回）**——改动小、零新依赖、立刻缓解面经的上下文爆炸问题，作为 RAG 的"地基"。
3. 确认收益后再决定是否进入 Phase 2（向量）。

> 本方案不急于落地，先评审。需要我细化某一 Phase 的接口设计或伪代码，或先就 Phase 1 出最小实现，请告知。
