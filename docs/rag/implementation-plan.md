# CountBot RAG 检索增强 · 开发落实计划（Implementation Plan）

| 项 | 值 |
|----|----|
| 文档版本 | v1.0 |
| 状态 | 待评审（Phase 0 为阻塞项） |
| 分支基线 | `pr/rag-crag-refusal` @ 235c6c0 |
| 配套文档 | 战略与选型论证见 `docs/rag/optimization-roadmap.md`；里程碑历史见 `docs/rag/archive/milestones.md` |
| 目标读者 | 拿到本文件即可直接开工的开发者 |
| 更新日期 | 2026-09-04 |

---

## 0. 检查结论（Review Findings）—— 必须先读

本文件基于对仓库真实代码的逐文件核查产出。核查发现 **1 个 P0 阻塞缺陷 + 2 处需在开工前统一口径的偏差**。

### P0 · 阻塞缺陷：`provider.chat_completion` API 断裂

**现象**：`backend/modules/wiki/tool.py`（232 / 336 / 378 / 417 行）与 `backend/api/wiki.py`（338 / 729 行）共 6 处调用 `provider.chat_completion(...)`，但 **该方法是死 API**：

- `LLMProvider` 基类（`backend/modules/providers/base.py`）只定义了 `chat_stream`（流式，`messages: List[dict]` 入参），**没有 `chat_completion`**。
- `OpenAIProvider` / `AnthropicProvider`（工厂 `create_provider` 的唯二返回类型）**均未定义** `chat_completion`。
- 仓库历史中 `backend/` 下**从未存在过** `def chat_completion`（`git log -S "def chat_completion" -- backend/` 为空）。

**影响**：生产环境下（`get_shared_provider()` 返回真实 provider），`_grade_chunks` / `_rewrite_query` / `_generate_from_chunks` 及文档级 `_compile_answer` 的 `provider.chat_completion(...)` 都会抛 `AttributeError`，被各自 try/except 吞掉后**静默回退到 `_rag_search`（原始块列表输出）**。即：

> **CRAG 纠偏路由与 LLM 生成在生产环境是 no-op。** 现有「负样本拒答 10/10、正样本 46/50」的评测数字是在 `run_crag_eval.py` 注入 `SimpleProvider`（自带了 `chat_completion`）的**离线环境**测得的，**不代表生产真实行为**。

**结论**：Phase 1 之前必须先修（列为 Phase 0），否则后续所有「grader 输入精排块」的假设都不成立。

### 口径修正 1 · Embedding 选型：FlagEmbedding 本地 → BGE-M3 经 API

原 M2 规划（`milestones.md`）与 `requirements-eval.txt` 注释预留的是「本地 FlagEmbedding + bge-small-zh-v1.5」。本计划改为 **BGE-M3 经 OpenAI 兼容 `/v1/embeddings` 端点调用**。理由：零本地 torch 依赖（符合模块「零第三方依赖」哲学）、C-MTEB 中文检索第一梯队、8K 上下文够用。FlagEmbedding 保留为离线降级备选（注释保留，不启用）。

### 口径修正 2 · Grader 职责：Phase 2 收缩为 all / none

现有 grader 输出 `all|partial|none` 并在 `partial` 时用 `relevant` 过滤块。Phase 2 引入 reranker 后，块过滤职责移交 reranker，grader 收缩为 `all|none`（只做拒答决策）。这是 Phase 2 的明确改动点，非当前基线问题。

---

## 1. 范围与目标

**范围**：Wiki 知识库问答的检索链路（分块 → 检索 → 重排 → 生成）升级。不触碰 `AgentLoop` / `ToolRegistry` / `Cron`（M0 硬约束延续）。

**目标**（量化，对照基线）：

| 指标 | 当前基线 | Phase 1 后 | Phase 2 后 |
|------|---------|-----------|-----------|
| 正样本误拒答 | 4/50 | ≤1/50 | 0/50 |
| 带块级引用率 | 72% | ≥85% | ≥90% |
| needle+cross_doc 子集 Hit@6 | 待测（基线缺失，Phase 1 第①步补） | +15pp | +25pp |
| MRR@6（60 题） | 待测 | 建立基线 | +20%（相对） |
| 单题平均 LLM 调用 | 2.2 次 | 不劣化 | ≤1.5 次 |
| 端到端 p95 | 未测 | 记录 | ≤3s |

**不做的（范围纪律）**：Qdrant / Milvus（规模不匹配）；GraphRAG（语料无实体网络）；LangGraph（CountBot 自身即 Agent 框架）；Contextual Retrieval（标题路径已提供上下文，写触发条件见 §9）。

---

## 2. 术语与约定

| 术语 | 含义 |
|------|------|
| slug | 文档唯一标识（如 `advanced/auth`），对应 `concepts/<slug>.md` |
| chunk_id | 块唯一标识 = `{slug}#{section-anchor}`（如 `advanced/auth#安装`） |
| est_tokens | `len(content) / 1.5`（CJK 近似，与 rag-bench 基线一致） |
| G0/G1/G2/G3 | 评测轮次：G0=文档级 BM25，G1=分块 BM25（已完成），G2=纯向量，G3=BM25+向量 RRF |
| L1/L2/L3 | 评测层级：L1=检索指标（ranx，程序化），L2=端到端（RAGAS），L3=系统对照（自建） |
| 停级条款 | 门禁不达标的合法退出路径：记录结论、不强行推进 |

**关键映射（已核实）**：`questions.jsonl` 的 `source_pages`（如 `core/memory.md`）去掉 `.md` 后缀即精确等于 `manifest.json` 的 `slug`。全 60 题 100% 可映射。chunk 级 qrels 即据此构造。

---

## 3. 现状基线（代码地图）

| 文件 | 关键符号 | 职责 | 行号 |
|------|---------|------|------|
| `backend/modules/rag/chunker.py` | `HeadingChunker`、`Chunk` | 标题切分 + 超长节二次切分（max 1200 / min 80 / overlap 120） | 52 / 19 |
| `backend/modules/rag/stores/bm25_store.py` | `ChunkedBM25Index` | 分块 BM25，`search_chunks()` / `add_document()` / `save_to_file()` | 20 / 85 / 35 / 123 |
| `backend/modules/rag/service.py` | `RagService` | 增量同步 + `search_chunks()` 门面 | 17 / 93 |
| `backend/modules/wiki/tool.py` | `WikiTool` | `_rag_search` 238 / `_rag_ask` 262 / `_grade_chunks` 307 / `_rewrite_query` 367 / `_generate_from_chunks` 392 | — |
| `backend/modules/wiki/index.py` | `BM25Index` | jieba 分词、标题×3 / 标签×2、倒排索引（被 ChunkedBM25Index 复用） | 29 |
| `backend/modules/providers/base.py` | `LLMProvider` | **仅有 `chat_stream`，缺 `chat_completion`（P0）** | 53 |
| `backend/app.py` | `get_shared_provider()` | 返回 `app.state.shared["provider"]` | 436 |
| `rag-bench/scripts/l1_eval.py` | `build_bm25_run` / `summarize` | ranx 检索评测；**注释已预留「G2 需新增 build_*_run」** | 124 / 181 |
| `rag-bench/scripts/run_crag_eval.py` | `SimpleProvider` / `main` | CRAG 端到端评测（注入 `chat_completion`） | 31 / 70 |
| `rag-bench/eval_config.yaml` | `l1.runs` | 已定义 G2=纯向量 / G3=RRF；L2 RAGAS 门禁 | 83 / 112 |

**基线数字**：52 文档 → 1,423 块；注入 12,334 → 910 est tokens（−92.6%）；CRAG 离线评测拒答 10/10、正样本 46/50、引用 72%、单题均 2.2 次 LLM 调用；G1 诚实回退：S3-07 针题 miss、S1-16/18 语义题跌出 top10、S2 全源命中 0.20→0.13。

---

## 4. 目标态架构（数据流）

```
离线（写路径，增量触发）                         在线（读路径，ask）
─────────────────────────                    ─────────────────────────
concepts/*.md  (mtime+content_hash)           用户问题
   │                                             │
   ▼                                             ▼
HeadingChunker  ──►  Chunk 池(1,423)      HybridRetriever
   │                     │                     │  ├─ BM25 top-50
   │                     ├──► BM25 倒排          │  └─ Dense top-50
   │                     │                      ▼
   │                     └──► embeddings.py   RRF(k=60) 融合
   │                          (BGE-M3 API)       │
   │                              │             ▼
   │                              ▼         Reranker (bge-reranker-v2-m3)
   ▼                         vector_store       │  top-50 → top-6
chunk_index.json ◄────────── (numpy)            ▼
vector_index.*  ◄──────────                     │
                                          LLM Grader (all/none)
                                                │
                                     ┌──────────┴──────────┐
                                   all                     none
                                     │                      │
                                     ▼                      ▼
                                生成(带引用)        改写×1 → 再检索
                                                        │
                                                  still none?
                                                     │
                                                   拒答
```

三层防御：① 检索双通道互补（BM25 管精确标识符，Dense 管语义变体）；② 精排可回退（reranker 失败 → RRF 排序）；③ 编排降级链（grader 失败 → 直出，任何一环未配置逐级回落到上一已验证形态）。

---

## 5. 分阶段开发计划

### Phase 0（紧急 · 0.5 天）修复 `chat_completion` API 断裂

| # | 文件 | 改动 | 规格 |
|---|------|------|------|
| 0.1 | `backend/modules/providers/base.py` | 在 `LLMProvider` 新增非抽象便捷方法 | `async def chat_completion(self, prompt: str, max_tokens: int = 2000, temperature: float = 0.3) -> str` |
| 0.2 | 同上 | 实现体：`prompt` 包成 `[{"role":"user","content":prompt}]`，`async for chunk in self.chat_stream(messages, max_tokens=..., temperature=...):` 累积 `chunk.content`，遇 `chunk.is_error` 抛 `RuntimeError(chunk.error)`，结束返回 `"".join(parts)` | — |
| 0.3 | `tests/rag/test_provider_completion.py`（新建） | 用带 `chat_stream` 的假 provider 断言拼接正确、错误传播正确 | ≥4 用例 |

**验收门禁**：
1. 全量 `tests/` 绿（138 + 新增）；
2. 用真实 provider（或等价 stub）跑 `run_crag_eval.py`，确认 grader 返回了 `all/partial/none` 而非恒回退（判据：`detail` 里出现非列表式答案、`llm_calls > 60`）。

### Phase 1（~1 周）检索指标基线 + M2 混合检索

#### 任务 1：检索层指标基线（先补盲区，半天）

| 文件 | 改动 | 规格 |
|------|------|------|
| `rag-bench/scripts/l1_eval.py` | 新增 `build_qrels_from_questions()` | 读 `questions.jsonl`，每题 `relevant` = 其 `source_pages`（去 `.md`）对应 slug 的全部 chunk_id（从 `ChunkedBM25Index` 枚举） |
| 同上 | 新增 `build_chunk_run()` | 用 `ChunkedBM25Index` 生成 TREC run（绕过阈值，同 `build_bm25_run` 的 `bypass_threshold` 逻辑） |
| `rag-bench/eval_config.yaml` | `l1.metrics` 追加 | `hit_rate@6`、`mrr@6`、`recall@6`（对齐生成 top-6 口径） |

> 说明：检索指标**零 LLM 成本、纯程序化**。此任务补上「Hit@6 / MRR 基线缺失」的盲区——没有它，后续任何检索改动都无法归因。

**验收**：能对 60 题输出 G1 分块 BM25 的 Hit@6 / MRR@6 基线表；与 G1 已知回退（S3-07 / S1-16/18 / S2）能对上号。

#### 任务 2：embeddings.py（1 天）

| 文件 | 改动 | 规格 |
|------|------|------|
| `backend/modules/rag/embeddings.py`（新建） | `class EmbeddingClient` | `async def embed_texts(self, texts: List[str]) -> List[List[float]]`；`async def embed_query(self, text: str) -> List[float]`（= `embed_texts([text])[0]`） |
| 同上 | `build_embedding_client() -> Optional[EmbeddingClient]` | 从 env 读取（见 §7），未配置返回 `None`；用 `httpx`（已是直接依赖）打 OpenAI 兼容 `/v1/embeddings`，`input` 批量 ≤16 |

#### 任务 3：vector_store.py（1 天）

| 文件 | 改动 | 规格 |
|------|------|------|
| `backend/modules/rag/stores/vector_store.py`（新建） | `class VectorStore` | `add(chunk_id, vec)` / `remove(chunk_id)` / `search(query_vec, top_k) -> List[Tuple[str,float]]`（余弦）/ `save_to_file(path)` / `load_from_file(path) -> bool` / `__len__` |
| 同上 | 实现 | **numpy 优先、纯 Python 兜底**（`try: import numpy`，否则 `math` 手写点积）。持久化 `workspace/wiki/vector_index.npz`（numpy）或 `.json`（兜底） |

#### 任务 4：retriever.py + service 接线（1 天）

| 文件 | 改动 | 规格 |
|------|------|------|
| `backend/modules/rag/retriever.py`（新建） | `rrf_fusion(ranked_lists, k=60) -> List[str]` | `score(d)=Σ 1/(k+rank_i(d))`，纯函数 |
| 同上 | `class HybridRetriever` | `async def search(self, query, top_k=6) -> List[dict]`；内部 BM25 top-50 + Dense top-50 → RRF → top-6；返回结构与 `search_chunks` 一致 |
| `backend/modules/rag/service.py` | `search_chunks` 分派 | embedding 客户端存在时走 HybridRetriever，否则纯 BM25；同步逻辑里对新增/变更块同步写向量 |
| `backend/modules/wiki/tool.py` | 零改动 | `search_chunks` 接口不变，`_rag_ask` / `_rag_search` 无需改 |

**Phase 1 验收门禁**（门禁 1 判定口径已于 2026-09-05 定稿，验收时照此执行、不再临时解释；题型含义见 `questions.jsonl`：single_doc 25 = 单文档直问，cross_doc 15 = 跨文档拼凑（语义改写多、关键词难对上），needle 10 = 长文档里捞一句原话，negative 10 = 语料中不存在的能力）：

1. **门禁 1（主判据：needle + cross_doc 合并口径）**：把 **needle（10 题）+ cross_doc（15 题）合成一个 25 题池**，整个池子的 Hit@6 较 G1 基线 **提升 ≥ +15pp**。
   - 术语解释（首次出现）：Hit@6 = 检索返回的前 6 条结果里是否含正确答案（含 = 1 分 / 不含 = 0 分），对池内每题平均后是 0~1 的命中率；pp = percentage point，百分点，+15pp 即命中率上升 15 个百分点（例如 0.40 → 0.55）。
   - **为什么按合并口径，而不是两类分开各算各的**：两类共享同一次检索升级，检验的是"混合检索相对纯 BM25 的整体增益"。合并成 25 题后，单题波动只有 ±4pp；若分开，cross_doc（15 题）单题 ±6.7pp、needle（10 题）单题 ±10pp——10 题的小池子会被一两道题的运气主导，而真实增益又集中在 cross_doc（语义改写要靠向量补），needle（关键词可直中）可能持平，分开判会因 needle 无提升误拒一个真实有效的改进。分开各 +15pp 等价于两个小样本检验做 AND，统计功效最低。
   - **佐证指标（同池，mrr@6）**：同一 25 题池的 MRR@6 较 G1 基线须为正提升。MRR@6 = Mean Reciprocal Rank，把"第一个正确答案在结果里的排名倒数"取平均（排在越前分越高），是连续指标、比"命中与否"的二分 Hit@6 对排序更敏感；二者同向才放行，防 25 题小样本下单个指标偶发漂移导致误判。
   - **诊断看板（不设硬门槛）**：needle 与 cross_doc 分开的 Hit@6 / MRR@6 照常列入报告，用于定位增益来源、为 Phase 3 触发条件（§5 3.1 / 3.2）提供依据，但不作放行/拦截条件。
2. 正样本误拒答 ≤1/50；引用率 ≥85%（重跑 `run_crag_eval.py`）；
3. **停级条款**：若 G2 纯向量对 S1 语义题无增量 → 记录「该语料用不上向量」为有效结论，停在此级，不强行上向量；
4. 无 embedding 配置时行为与现状完全一致（降级安全）；全量测试绿。

### Phase 2（2~3 周）重排 + grader 职责收缩

| # | 文件 | 改动 | 规格 |
|---|------|------|------|
| 2.1 | `backend/modules/rag/reranker.py`（新建） | `class RerankerClient` | `async def rerank(self, query, chunks: List[dict]) -> List[dict]`；打 OpenAI 兼容 `/v1/rerank`（SiliconFlow 风格），按 `relevance_score` 降序取 `top_n=6` |
| 2.2 | 同上 | `build_reranker() -> Optional[RerankerClient]` | env 未配置返回 `None` |
| 2.3 | `backend/modules/wiki/tool.py` | `_rag_ask` 改造 | `search_chunks(top_k=50)` → rerank → top-6 → grader → generate；reranker 失败回落 RRF 排序 |
| 2.4 | `backend/modules/wiki/tool.py` | `_grade_chunks` 收缩 | 输出改为 `{"grade":"all|none"}`，删除 `partial` 分支与 `_filter_chunks` 调用；prompt 同步收缩 |
| 2.5 | 缓存 | grader/rerank 结果 LRU | key = `(query, tuple(sorted(chunk_ids)))`，容量 ~256 |

**Phase 2 验收门禁**：
1. MRR@6 相对提升 ≥20%；rerank 后 top-6 Hit@6 ≥90%（正样本）；
2. 单题 LLM 调用均值 2.2 → ≤1.5 次；grader prompt 收缩 ≥40%；
3. 端到端 p95 ≤3s；60 题全量回归：拒答 10/10 不回退，正样本 ≥48/50。

### Phase 3（按需推进，触发条件见 §9）

| # | 改动 | 触发条件 |
|---|------|---------|
| 3.1 | 跨文档 map-reduce 路由（查询分解：ask 前置 `scope: single|aggregate` 判断，aggregate → 按文档拆子查询 → 各自检索 → 归并生成） | Phase 2 后 cross_doc 全源命中仍 <0.5 |
| 3.2 | Contextual Retrieval（对失败块做 LLM 上下文前置） | needle 类 Hit@6 不达标且失败集中在「块内无查询关键词」 |
| 3.3 | M3 语义记忆 / M4 时序聚合 | 与原里程碑节奏独立推进，不阻塞本链路 |

---

## 6. 数据结构与接口规范

### 6.1 chunk 结果结构（`search_chunks` / HybridRetriever 统一返回）

```json
{
  "chunk_id": "advanced/auth#安装",
  "slug": "advanced/auth",
  "doc_title": "远程访问认证",
  "section": "安装",
  "score": 0.83,
  "content": "..."
}
```

### 6.2 向量持久化（`vector_index.npz`，numpy 版）

```
keys: chunk_ids（str 数组）、vectors（float32 [N, dim]）
dim = COUNTBOT_RAG_EMBEDDING_DIM（默认 1024）
```

### 6.3 grader 输出契约（Phase 2 收缩后）

```json
{"grade": "all|none"}
```

### 6.4 评测 qrels（chunk 级，TREC 格式，由 `build_qrels_from_questions` 生成）

```
<qid> 0 <chunk_id> 1
```

---

## 7. 配置项与环境变量清单

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `COUNTBOT_RAG_CHUNKS` | 关 | M1 分块检索开关（既有，`1/true/yes/on` 开启） |
| `COUNTBOT_RAG_HYBRID` | 关 | Phase 1 混合检索开关；关 = 纯 BM25 |
| `COUNTBOT_RAG_EMBEDDING_BASE_URL` | — | 嵌入端点（OpenAI 兼容，如 `https://api.siliconflow.cn/v1`） |
| `COUNTBOT_RAG_EMBEDDING_API_KEY` | — | 嵌入 API Key |
| `COUNTBOT_RAG_EMBEDDING_MODEL` | `BAAI/bge-m3` | 嵌入模型 |
| `COUNTBOT_RAG_EMBEDDING_DIM` | `1024` | 向量维度 |
| `COUNTBOT_RAG_RERANK` | 关 | Phase 2 重排开关 |
| `COUNTBOT_RAG_RERANK_BASE_URL` | — | 重排端点 |
| `COUNTBOT_RAG_RERANK_API_KEY` | — | 重排 API Key |
| `COUNTBOT_RAG_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 重排模型 |

> 依赖改动：`requirements.txt` 新增 `numpy>=1.26`（`httpx` 已有，第 15 行；`openai` 已有，第 20 行）。若坚持「零新增依赖」，vector_store 走纯 Python 兜底分支即可，`numpy` 改为可选。

---

## 8. 测试规格

| 层 | 文件 | 覆盖点 | 目标 |
|----|------|--------|------|
| 单元 | `tests/rag/test_provider_completion.py` | chat_completion 拼接 / 错误传播 / 空流 | ≥4 |
| 单元 | `tests/rag/test_embeddings.py` | Noop 降级 / 批量切分 / 维度校验 | ≥5 |
| 单元 | `tests/rag/test_vector_store.py` | 增删 / 余弦搜索排序 / 持久化 round-trip / 纯 Python 兜底 | ≥6 |
| 单元 | `tests/rag/test_retriever.py` | RRF 纯函数 / 混合返回结构 / 无向量时回落 BM25 | ≥5 |
| 单元 | `tests/rag/test_reranker.py` | 排序正确 / 失败回落 / 空候选 | ≥4 |
| 回归 | `tests/`（全量） | 每 Phase 结束全绿，不劣化现有 138 | 138+ |
| 评测 | `rag-bench/scripts/l1_eval.py` | 每 Phase 前后跑 chunk 级 Hit@6 / MRR@6 | 对照表 |
| 评测 | `rag-bench/scripts/run_crag_eval.py` | 每 Phase 前后跑端到端拒答 / 引用率 | 对照表 |

---

## 9. 风险、回退与停级条款

| 风险 | 概率 | 对策 |
|------|------|------|
| `chat_completion` 修复引入 provider 行为变化 | 低 | 便捷方法包裹 `chat_stream`，不改流式路径；全量回归覆盖 |
| BGE-M3 API 不可用 / 抖动 | 中 | `build_embedding_client` 返回 None → 纯 BM25；嵌入结果持久化，查询期仅 embed 问题本身 |
| rerank API 延迟超标 | 中 | LRU 缓存 + 候选上限 50 + 回落 RRF |
| 混合检索对 S1 无提升 | 低 | **停级条款**：记录为有效结论，符合项目既有纪律 |
| 向量与 BM25 索引漂移 | 低 | 共享 slug registry 与 mtime 校验，同一 sync 事务内双写 |
| grader 收缩后拒答率回退 | 低 | Phase 2 门禁显式盯 10/10 拒答不回退 |

**停级条款（总则）**：任何门禁不达标 → 回退到上一已验证形态并记录原因，「停在某一级也是有效结论」。

**Contextual Retrieval 触发条件**（写死，避免焦虑驱动）：Phase 2 后 needle 类 Hit@6 不达标，**且**失败案例集中在「块内无查询关键词」时才做；对失败案例做 contextual enrichment 再评测，用数据决定去留。

---

## 10. 关键文件行号索引

| 目标 | 位置 |
|------|------|
| P0 修复点（缺 `chat_completion`） | `backend/modules/providers/base.py`（LLMProvider 类，第 53 行起） |
| 6 处死 API 调用 | `backend/modules/wiki/tool.py` 232/336/378/417；`backend/api/wiki.py` 338/729 |
| 检索门面（改混合分派） | `backend/modules/rag/service.py` `search_chunks`（93） |
| 分块 BM25（复用） | `backend/modules/rag/stores/bm25_store.py` |
| 评测扩展点（已留注释） | `rag-bench/scripts/l1_eval.py` `build_bm25_run`（124） |
| G2/G3 轮次与 L2 门禁定义 | `rag-bench/eval_config.yaml`（83 / 112） |
| 评测依赖（已注释 FlagEmbedding/ragas） | `rag-bench/requirements-eval.txt` |

---

## 附：推荐阅读顺序

1. **§0 检查结论** —— P0 阻塞项与口径修正，开工前必读
2. **§5 Phase 0** —— 立即动手的修复清单
3. **§5 Phase 1 任务 1~4** —— 下周开发主战场
4. **§6 / §7** —— 接口与配置规格（开发时对照）
5. **§8 / §9** —— 测试与回退纪律
6. 战略与「为什么这么选」的完整论证 → 回看 `docs/rag/optimization-roadmap.md`
