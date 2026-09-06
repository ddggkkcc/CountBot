# CountBot RAG 优化 · 工程日志（Work Log）

> 定位：个人工程记录，用于复盘与社招叙述的**事实底稿**。所有数字来自 `rag-bench/results/` 与各分支真实评测，可复现。
> 本文先保证"讲的是真的"，"怎么讲"见 §8；对外交付（issue/PR）只使用其中被引用的部分。
> 状态标记：✅ 已完成 ｜ 🔄 进行中 ｜ ⏸ 暂缓（有触发条件）｜ ❌ 明确不做（有论证）

---

## 0. 一页纸摘要（电梯陈述）

**项目**：给开源 AI Agent 框架 CountBot（countbot-ai/CountBot，issue #107，维护者已确认接受 PR）的 Wiki 知识库做 RAG 升级——从"BM25 整篇文档召回直接塞 prompt"升级为"标题分块检索 + CRAG 纠偏路由（可拒答）+ 混合检索演进中"。

**我的角色**：独立完成 分析 → 设计 → 实现 → 评测 → 上游 PR 全流程。

**核心成果（全部为同一 60 题评测集上的 before/after）**：

| 指标 | 升级前 | 升级后 | 变化 |
|---|---|---|---|
| 注入上下文（avg est tokens） | 12,334 | 910 | **−92.6%** |
| 注入上下文（max） | 45,944 | 1,720 | −96.3% |
| 正样本生产命中率 | 28/50 (56%) | 46/50 (92%) | +36pp |
| 负样本拒答率（防幻觉） | 0/10（全部幻觉作答） | 10/10 | +100pp |
| 答案块级引用率（可溯源） | 0% | 72% | — |
| 直接提问 top1 命中 | 0.067 | 0.533 | ×8 |

**工程特色（面试差异化）**：评测先行（60 题四分层 + 三层指标体系 + 门禁与停级纪律）、零新依赖实现分块检索、纠偏路由零破坏降级设计、每个"不做"都有数据论证。

---

## 1. 背景与问题定义（2026-08）

代码级审计发现的三个真实问题（不是模板痛点）：

1. **整篇召回打爆上下文**：`wiki/tool.py` 的 ask 走 BM25 top-3 整篇注入，平均 12,334 est tokens，最大 45,944（约占全语料五分之一）；
2. **词法检索过不了语义鸿沟**：用户换个措辞（"到点自动干活" vs 文档"定时任务"）即 miss，paraphrase recall@5 仅 0.20；
3. **语料里没有就幻觉**：负样本问题（K8s 部署？手机 App？）0/10 拒答，全部编造。

关键判断：CountBot 已有 BM25 + LLM 生成闭环，**这不是"要不要加 RAG"而是"升级检索单元 + 补编排层"**。升级路径按 ROI 排序：分块（立刻解决 1）→ 纠偏路由（解决 3）→ 混合检索（解决 2）→ 重排。

---

## 2. 时间线与阶段产出

| 阶段 | 时间 | 分支 | 交付 | 门禁结果 |
|---|---|---|---|---|
| M0 基线 | 2026-08 | feature/rag-enhancement | 60 题评测集（S1 单文档 25 / S2 跨文档 15 / S3 针题 10 / S4 负样本 10）+ 语料 C1–C3 + G0 基线 | 基线可复现 ✅ |
| M1 分块检索 | 2026-08-28 | feature/rag-enhancement | `backend/modules/rag/`（HeadingChunker / ChunkedBM25Index / 增量同步）+ `COUNTBOT_RAG_CHUNKS` 开关 | 4/4 门禁全过 ✅ |
| CRAG 拒答路由 | ~2026-09-03 | pr/rag-crag-refusal @235c6c0（已合入） | `_rag_ask`：grade → rewrite → refuse 状态机 | 拒答 10/10，正样本 46/50 ✅ |
| Phase 1 检索基线 | 2026-09-05 🔄 | pr/rag-phase1-retrieval-baseline | L1 块级指标（hit_rate@6 0.82 / mrr@6 0.647）+ 评测架构 v2 + embeddings/vector_store（见 §2.3） | 进行中 |

> 详细规划见 `docs/rag/optimization-roadmap.md`（战略）与 `implementation-plan.md`（开发规格）；上游 PR 策略见 `pr-submission-handbook.md`。

### 2.1 M1：分块检索（零新依赖先解决最大痛点）

- **做了什么**：`HeadingChunker` 按 Markdown 标题层级切分（超长节按段落二次切分，max 1200 字符 / overlap 120，代码围栏与表格不可分割），每块携带 `slug#section` 溯源；`ChunkedBM25Index` 复用上游 `BM25Index` 做块级倒排；`RagService` 用 mtime 做增量同步、单文档重建钩子；`wiki/tool.py` 加开关委托，**默认关闭 = 零行为变化**。
- **结果**：52 文档 → 1,423 块；注入 token −92.6%；命中率 56% → 82%；引用率 0% → 82%；测试 35/35 全绿。
- **诚实记录的回退**（G1 实验报告）：S3-07 针题 miss；S1-16/18 语义变体题跌出 top10（BM25 固有上限，M2 的动机）；S2 跨文档全源命中率 0.20 → 0.13（top-6 结构上覆盖不了多源题，Phase 3 的动机）。**把回退写进报告而不是藏起来**——这是后续所有"为什么做 M2/Phase 3"的依据。

### 2.2 CRAG：纠偏路由（编排层，防幻觉）

- **做了什么**：检索 top-6 → LLM grader 判 all/partial/none → partial 过滤块、none 改写查询重试一次 → 仍 none 则显式拒答。全程降级安全：无 provider / 解析失败回退直出（M1 行为）。
- **关键决策**：为什么用事后 grader 而不是前置分数门控——实测**负样本的 top-1 BM25 分与正样本重叠 7/10**，任何分数阈值都挡不住幻觉，语义判断只能交给 LLM 事后做。这个数字直接否定了"分级前置路由"方案（roadmap §3.7）。
- **结果**：负样本拒答 0/10 → 10/10；正样本 46/50（4 题误拒答）；引用 72%；单题 LLM 调用 2.2 次（grader + 生成 + 重写路径）。

### 2.3 Phase 1：混合检索与评测基线（进行中，分支 pr/rag-phase1-retrieval-baseline）

- **任务 1 ✅（6c6972d）**：检索层指标基线——`l1_eval.py` 块级 qrels（`source_pages` 文档级标注自然扩张到块级，slug 映射已核实 100% 可映射）+ top-6 口径指标：**hit_rate@6 0.82 / mrr@6 0.6473 / recall@6 0.0801**，程序化打分零 LLM 成本。此后任何检索改动的归因有了 before 数字。
- **任务 2 ✅（dfcde76）**：`embeddings.py`——OpenAI 兼容 `/v1/embeddings` 客户端（BGE-M3）。批量 ≤16 按响应 `index` 字段重排（乱序/缺失/越界/重复全部拒绝）；维度与 `COUNTBOT_RAG_EMBEDDING_DIM` 校验不符即 loud fail（错模型/错维度必须报错而不是静默污染向量索引）；未配置返回 `None` 走纯 BM25（降级安全契约）；httpx 直调零新依赖。
- **任务 3 ✅（10236ea）**：`stores/vector_store.py`——numpy 优先 / 纯 Python 兜底的余弦 top-k。当前规模（1,423 块 × 1024 维）暴力检索毫秒级，明确不上 ANN（规模不匹配）；`.npz`/`.json` 双持久化按内容自动识别（两种环境互相可读）；`add` 幂等（服务增量同步）、`remove` O(1) swap-delete；零向量块（纯代码围栏）相似度定义为 0 而非报错。
- **评测架构 v2 ✅（2026-09-05）**：见 §4.3。
- **任务 4 ⏳（2026-09-05 实测修订，见 §5 存档）**：`retriever.py` HybridRRF（BM25 top-50 ⊕ Dense top-50 → RRF k=60 → top-6）+ `service.py` 接线 + `COUNTBOT_RAG_HYBRID` 开关。BGE-M3 实测后：RRF 融合在 top-6 注入窗口上是净亏（hit 43→40），形态修订为"RRF 只做候选兜底（union top-50），top-50→top-6 精排交给 Phase 2 rerank"。

### 2.4 Phase 2：重排 + grader 职责收缩（进行中）

- **任务 2.1/2.2 ✅（340a676）**：`reranker.py`——OpenAI 兼容 `/rerank` 客户端（bge-reranker-v2-m3），防御性排序+截断 top_n、`rerank_score` 与检索分分离保留、`COUNTBOT_RAG_RERANK` 开关（默认关）。
- **任务 2.3 ✅（340a676）**：`_rag_ask` 改造——检索 top-50（`min_score_ratio=0`，候选质量归 reranker 把关）→ rerank top-6 → grader → 生成；**置信门控**：rerank top-1 ≥ 0.7 时跳过 grader 直出（单题 LLM 调用 ≤1.5 门禁的实现手段，阈值待 60 题回归校准）；rerank 失败回落检索排序且必走 grader（降级不裸奔）。
- **任务 2.4 ✅（340a676）**：grader 收缩为 `all|none` 拒答判定，删除 partial 分支与 `_filter_chunks`（废弃输出按不可解析回退全量生成）；prompt 收缩约 40%。
- **任务 2.5 ✅（340a676）**：grader/rerank 结果 LRU（容量 256，key=query+chunk_id 集），失败不缓存。
- **测试**：211 全绿（新增 13：reranker 客户端契约 / 置信门控 / 失败回落 / 缓存行为 / 收缩后解析契约 / 门控默认禁用）。
- **置信门控被冒烟校准证伪（2026-09-06，记录在案）**：设计项"rerank top-1 高分跳过 grader"上线前用两条冒烟查询校准——负样本"部署到 Kubernetes"对 Docker 部署块 **0.9933**，正样本"如何用 Docker 部署"对同块 **0.9046**：cross-encoder 度量主题匹配而非可回答性，**任何分数阈值都无法分离**（与 D2"负样本分数重叠、门控挡不住"结论换通道后依然成立）。处置：阈值默认 1.01 = 禁用（机制与测试保留，monkeypatch 可验）；"单题调用 ≤1.5"门禁暂不达成（实测约 2.0/题：grader+生成），诚实记录，待有判别力的信号（如多块分数分布形态）再启用。**教训进方法论：所有"分数阈值"类门控上线前必须先做正负样本分离度冒烟，成本两条查询。**
- **⏳ 待办**：rerank 端点实测验收（SiliconFlow bge-reranker-v2-m3，进行中）——MRR 相对 +20%、Hit@6 ≥90%、p95 ≤3s、60 题回归（拒答 10/10 不回退 + 正样本 ≥48/50）。→ 已完成，见下。

**Phase 2 验收结果存档（2026-09-06，BGE-M3 + bge-reranker-v2-m3 口径，`results/l1-p2-rerank/`、`results/crag-detail-v2-p2-rerank.json`；嵌入切回选型定的 BGE-M3）：**

检索层（L1，60 题同口径）：

| run | hit@6 | mrr@6 | cross_doc | needle | single_doc |
|---|---|---|---|---|---|
| G1 BM25 | 0.8200 | 0.6473 | 0.8667 (0.6689) | 0.9000 (0.7750) | 0.7600 (0.5833) |
| **G4 混合+重排** | **0.9000** | **0.7000** | 0.9333 (0.7556) | **1.0000** (0.8000) | 0.8400 (0.6267) |

端到端（v2 judge，pairwise 对照 G1 基线）：

| 指标 | G1 基线 | G3 混合单跑 | **P2 混合+重排** | 门禁 | 判定 |
|---|---|---|---|---|---|
| 负样本拒答 | 10/10 | 8/10（judge 假象） | **10/10** | 不回退 | ✅ |
| 正样本误拒答 | 10/50 | 10/50 | **5/50** | ≤1/50 | ❌ 未达标（减半） |
| 错误作答（wrong+hallu） | 2 | 2 | **4** | — | ⚠️ 翻倍，见下 |
| 质量通过（correct+partial） | 35/50 | 37/50 | **39/50（78%）** | — | ↑ |
| 带引用 | 72% | 74% | **82%** | ≥85% | ❌ 未达标（+10pp） |
| pairwise vs G1 | — | win18/loss19/tie21 | **win23/loss9/tie26** | — | ✅ 显著转正 |
| judge/关键词一致率 | 0.8621 | 0.9123 | **0.9655** | — | ↑ |
| 单题调用 | 2.25 | 2.13 | 2.18 | ≤1.5 | ❌（门控证伪后禁用） |

**门禁汇总**：✅ Hit@6 0.90 ≥90%、拒答 10/10 不回退、pairwise 净赢 +14；❌ MRR 相对 +8.1%（强基线天花板，与 Phase 1 门禁修订同源教训）、误拒答 5/50、引用 82%、单题调用 2.18。

**最有价值的定性发现（诚实记录）**：误拒答 10→5 减半，但其中 3 题从"诚实拒答"变成"作答但答错"（S1-14/S3-07 针题：语料确实写了"22 个 provider"（`core/providers.md:40`），G3 拒答、P2 答成"两套具体 Provider 实现"；S2-06 同理）。根因是我为防误拒写进收缩后 grader prompt 的偏置"不确定时判 all（宁可生成，不误拒答）"**过强**——成功减半误拒，却把不确定性导向了幻觉。**质量通过率上升（37→39）掩盖了这个性质变化，只有把五分类 verdict 摊开看才能看到。**

**下一步（P2 收尾项，可交给开发窗口）**：把 grader 偏置从"宁可生成"调回"证据不足则拒答"，对事实/数字类（needle）问题要求"答不出具体事实即判 none"；重跑 60 题回归，确认误拒答与错误作答同时收敛。这正是 v2 judge 五分类的价值——二值指标（answered / refused）永远看不到这个权衡。

**口语集（30 题 Fuzzy）三档完整证据（2026-09-07，`questions-fuzzy.jsonl` / `results/l1-fuzzy-bge/` / `results/crag-detail-fuzzy-*.json`）**：

构造动机：响应上节 3c 的配比缺口（术语集语义题仅 ~20%），把"语义占比"推到 100% 模拟真实用户问法——**术语集是开发者视角，口语集才是用户视角**。

检索层（L1，BGE-M3 统一口径）：

| 口语 30 题 | G1 BM25 | G3 混合 | P2 混合+重排 |
|---|---|---|---|
| **Hit@6** | 0.3333 | 0.7000 | **0.8333** |
| **MRR@6** | 0.1867 | 0.4300 | **0.5194** |
| Recall@6 | 0.0158 | 0.0415 | 0.0701 |

端到端（v2 judge，pairwise 对照口语集 G1 基线）：

| 口语 30 题 | G1 | P2 |
|---|---|---|
| correct | 3 | **12** |
| partial | 7 | 13 |
| **refused** | **17（57%）** | **1（3%）** |
| wrong / hallucinated | 0 / 2 | 2 / 2 |
| 质量通过 | 10/30（33%） | **25/30（83%）** |
| 带引用 | 10 | 24 |
| 误拒答归因 | retrieval_miss 14 + grader_error 3 | grader_error 1（QF-28） |
| 系统调用/题 | 2.67 | 2.13 |
| **pairwise vs G1** | — | **win 21 / loss 7 / tie 2** |

**这是整个项目最有说服力的一组数字**：同一批口语提问下，词法路径 57% 直接拒答、只有 3 题答对；混合+重排把拒答压到 1 题、质量通过 33%→83%、pairwise 净赢 +14。对照术语集上"混合仅 +4pp"——**收益大小取决于查询分布，术语集的开发者视角严重低估了 RAG 升级的真实价值**。

两个诚实回退（记录不藏）：
1. **BGE-M3 在口语集上弱于 qwen3-embedding-8b**：混合 G3 的 Hit@6 0.70（BGE-M3）vs 0.80（qwen3-8b，同集早期口径），cross_doc 0.80 vs 0.93、needle 0.50 vs 0.60——**嵌入模型与查询分布存在交互**，术语集上的选型结论不能无条件外推到口语分布。
2. **P2 在 single_doc 上回退**（G3 0.80 → G4 0.60）：口语化措辞（如"拧哪个旋钮管它脑洞大小"）让 cross-encoder 排错了序；同时 needle 大涨（0.50→0.90，模糊指代被精排救回）。净 +13pp，但回退要留档——这是 Phase 2.5 可做的"口语查询改写前置"线索。

---

## 3. 关键决策记录（ADR 精简版）

每个决策都有"为什么不是别的"的论证，完整版在 `docs/rag/optimization-roadmap.md` §3。

| # | 决策 | 核心依据（一句话） | 被否掉的备选 |
|---|---|---|---|
| D1 | 检索单元：整篇 → 标题分块 | 最大痛点是上下文爆炸，分块零新依赖先解决它 | 直接上向量（把两个变量混在一起，不可归因） |
| D2 | CRAG 事后纠偏而非前置路由 | 负样本 top-1 分与正样本重叠 7/10，分数门控不可行 | 分级意图路由（LangChain adaptive-RAG 式） |
| D3 | 不引入向量数据库 | 1,423 块 × 1024 维 ≈ 5.6 MiB，numpy 暴力余弦 <5ms；引 Qdrant/Milvus 是纯运维负债 | Qdrant / Milvus / Chroma |
| D4 | RRF 融合而非加权分数和 | 只依赖排名不依赖分数，一个参数零校准，语料无关 | BM25/余弦加权融合（量纲不同，换语料静默失效） |
| D5 | BGE-M3 经 API 调用 | 中文第一梯队 + 零本地依赖 + MIT 许可 | Qwen3-Embedding-8B（要 GPU）、OpenAI（中文中等+海外依赖） |
| D6 | ❌ 不做 GraphRAG | 30 篇技术文档抽不出实体网络；跨文档总结用查询分解+map-reduce 覆盖 | — |
| D7 | ❌ 不引入 LangGraph | CountBot 本身是 Agent 框架，tool.py 的路由就是编排层；嵌套框架 = 双份状态管理 | — |
| D8 | ⏸ Contextual Retrieval 暂缓 | HeadingChunker 已带标题路径上下文（该技术要补的东西已结构化拥有）；**写明触发条件**：needle Hit@6 不达标且失败集中在"块内无查询关键词"时再上 | — |
| D9 | Phase 2 reranker 与 grader 重新分工 | reranker 管精排（毫秒级、无 token 成本），grader 管拒答（语义判断） | 继续 LLM grader 兼职过滤（token 隐形漏水点） |

**决策纪律**：一次只改一处（单变量原则）；每阶段有量化门禁，**门禁不过即回退**；"停在某一级也是有效结论"（M2 若语义变体题无提升，"该语料用不上向量"即为结论，写入 PR 不强行上）。

---

## 4. 评测方法论（独立于系统的第二资产）

### 4.1 三层指标金字塔

| 层 | 评什么 | 工具 | 特点 |
|---|---|---|---|
| L1 检索组件 | hit_rate@6 / MRR@6 / recall@6（对齐生产 top-6 注入口径） | ranx + 块级 qrels | 程序化打分零 LLM，CI 可回归，最可复现 |
| L2 端到端 | 拒答率 / 五分类 verdict / 引用率 / pairwise 胜负 | run_crag_eval.py v2 | LLM judge 固定模型版本 |
| L3 系统 | token 压缩比 / 成本 / p95 延迟 / 单题 LLM 调用数 | 自建脚本 | 公开基准无法提供，差异化证据 |

### 4.2 评测集设计（60 题）

- **四分层**：single_doc 25（直问 15 + 语义变体 10）/ cross_doc 15 / needle 10（精确数字针）/ negative 10（语料中不存在的能力，测拒答）；
- 每题标注 `source_pages`（→ 块级 qrels 自动构造）与 `expected_points`（→ judge 分点评分依据）；
- 语料全部来自官方文档站（countbot.cn/docs 52 页），**作者可复现，无私有数据**；
- 已知限制：cross_doc 仅 15 题方差大、语义变体仅 10 题——扩容方向已定（对齐 Phase 1 优化目标）。

### 4.3 评测架构 v2 升级（2026-09-05，本次迭代）

v1 的问题：拒答判定靠关键词匹配（换措辞即失效）、`expected_points` 标注了但没消费（无法区分"答了但答错"和"答对了"）、误拒答只能猜原因。v2 改动：

1. **LLM judge 消费 expected_points**：五分类 verdict（correct/partial/wrong/refused/hallucinated）+ 分点覆盖；
2. **关键词判定降级为 fallback**，并输出 judge/关键词一致率（judge 自身的元指标）；
3. **检索层信号 + 误拒答归因表**：每题记录原始问题第一次检索是否命中，正样本误拒答自动分流为 `retrieval_miss`（检索层问题）vs `grader_error`（编排层问题）——归因从猜测变成查表；
4. **pairwise 模式**：新旧 run 逐题 A/B 换序双判（一致才计票），Phase 验收用相对比较而非绝对分数（小 judge 模型上绝对分噪声大）；
5. **--judge-model 可与被测模型分离**（防同模型自评偏好）。

### 4.4 踩过的评测坑（都是真实数字）

| 坑 | 后果 | 处置 |
|---|---|---|
| BM25 双重阈值（相对 0.3 + 绝对 0.5）截断 top_k | recall@50 被严重低估，**测的是阈值行为不是检索能力** | 评测脚本 bypass_threshold，口径写死在 eval_config.yaml |
| jieba 未装回退单字分词 | recall@5 实测 0.80 → 0.53，基线失真 | 脚本启动检查 + requirements-eval.txt |
| 文档级标注 → 块级 qrels 的映射 | slug 不一致则 qrels 全空 | 核实 manifest slug == source_pages 去 .md 后逐字相等 |
| judge 模型/数据集版本漂移 | 跨版本分数不可比 | provenance 必填字段（judge 版本 / 数据集版本 / seed / 日期） |
| 推理型模型（deepseek-v4-pro）当 judge，思考计入 max_tokens | 300 预算全被思考耗尽（finish_reason=length、content 为空）→ judge 全部回退关键词 | judge/pairwise 的 max_tokens 提至 2500；换 judge 模型时先冒烟验证 |
| max_tokens=2500 仍偶发不够（L2 混合验收的 S4-06/S4-08） | verdict=unknown 回退关键词，负样本"漏拒"假象（答案实为合格软拒答） | judge fallback 率应进 summary 监控；后续识别 finish_reason=length 做重试或加预算 |
| 评测脚本 HTTP 调用无读超时 | 连接半开 → 进程挂起 2 小时无进展（CPU 15 秒/2 小时，无任何外部连接），**已跑完的 20 题全部作废** | `SimpleProvider` 加 `timeout=120.0, max_retries=1`；评测脚本必须有超时——挂起的代价是整轮评测而非一次重试延迟 |
| slug 归一化不一致（`core/memory` vs `core__memory`） | retrieval_hit 恒为 N，归因表全错 | expected_slugs 与语料构建做同样的 `/`→`__` 替换 |
| 评测集标注漂移（S1-01：标注写 JSON、语料是行式 MEMORY.md） | judge 判"幻觉"但实为标注错 | v2 judge 跑通即抓到 1 处；标注须对照语料核（retrieval_hit=Y 却被判 hallucinated/wrong 的题是筛查线索） |
| 生成层偶发空回复（~2-3%，跨 run 随机分布：v1 的 S2-11 / v2 的 S1-01、S1-14） | v1 关键词判定把空回答算作"answered"，**生产 bug 被评测口径掩盖** | v2 judge 暴露（判"系统回答为空"）；生产侧待修：`_generate_from_chunks` 空 content 应重试或回退 `_rag_search`，现仅异常才回退 |

---

## 5. 结果数字总表（单一事实来源：rag-bench/results/）

| 指标 | G0 整篇 BM25 | G1 分块 BM25 (M1) | + CRAG | 来源 |
|---|---|---|---|---|
| 注入 token avg / max | 12,334 / 45,944 | 910 / 1,720 | 同 G1 | g0/g1 报告 |
| 直问 top1 | 0.067 | 0.533 | — | g1-chunked.md |
| paraphrase recall@5 | 0.20 | 0.60 | — | g1-chunked.md |
| 正样本命中率 | 28/50 | 41/50 | 46/50 | 各期报告 |
| 负样本拒答 | — | 0/10 | 10/10 | crag-detail.json |
| 块级引用率 | 0% | 82% | 72% | 各期报告 |
| L1 hit_rate@6 / mrr@6 / recall@6 | — | 0.8200 / 0.6473 / 0.0801 | — | l1-g1-chunk/summary.md |
| S2 跨文档全源命中率 | 0.20 | 0.13 | 0.13 | g1-chunked.md（已知短板，Phase 3 靶子） |
| 单题 LLM 调用 | ~1 | ~1 | 2.2 | crag 报告 |

> recall@6 低是**块级 qrels 口径的自然结果**（每题 relevant = 源文档全部块，单次 top-6 填不满），不是回归信号；主看 hit_rate@6 与 mrr@6。

**v2 judge 口径基线（2026-09-05 存档：`rag-bench/results/crag-detail-v2-g1-baseline.json`，G1+CRAG，被测=deepseek-v4-flash，judge=deepseek-v4-pro，M2 验收的 pairwise 对照锚点）**：

| 指标 | 数值 | 说明 |
|---|---|---|
| 负样本拒答 | **10/10** | 与 v1 一致，CRAG 防幻觉守住 |
| 正样本误拒答 | **10/50**（v1 关键词口径只报 4/50） | v1 漏掉 5 题"软拒答"（非标准文案的"无法回答"）——真实起点比 v1 报告的更差，Phase 1 目标 ≤1/50 的分母重新校准 |
| verdict 分布 | correct 22 / partial 13 / wrong 2 / refused 10 / hallucinated 1 | wrong 含 2 题空回答（run artifact，偶发 bug）；hallucinated 为真幻觉（S3-09：语料只有 socks5，系统声称还支持 http） |
| 质量通过（correct+partial） | 35/50（70%） | v1 无此维度 |
| 误拒答归因 | retrieval_miss 5 题：S1-02/17/23、S2-14、S3-07；grader_error 5 题：S1-05/09/20/21、S2-15 | **retrieval_miss 组 = M2 混合检索的直接靶子**（S1-17 语义题、S3-07 针题均在其中）；grader_error 组 = Phase 2 reranker/grader 分工的靶子 |
| judge/关键词一致率 | 0.8621 | 8 题分歧全部可解释（软拒答 5 + 空回答 2 + 真幻觉 1），分歧处 judge 均更准 |
| 系统调用 | 135 次 ≈ 2.25 次/题 | 与 v1 的 2.2 一致，确认基线系统行为无漂移 |

**BGE-M3 检索层实测与门禁修订存档（2026-09-05 晚间，49 正题去重口径，`rag-bench/results/l1-g123-bge/`）**：

两件事先存档（诚实记录）：
1. **评测 embedding 与选型不一致**：G2/G3 首轮 runs 用了 `COUNTBOT_RAG_EMBEDDING_MODEL=qwen/qwen3-embedding-8b`（shell 残留 env），而选型定稿是 BAAI/bge-m3（roadmap §3.1 / `embeddings.py` 默认值）。换 BGE-M3 重跑后 dense 明显变强（mrr@6 0.5963→0.6540；cross_doc 14/15→15/15）——**换 embedding 之前不能给混合检索下结论**。
2. **重复题污染**：S3-07 与 S1-14 同题（同问句同 source_page，仅题型标签不同），已在 `questions.jsonl` 标 `dup_of: S1-14`；汇总口径 60→49 正题。

实测（hit@6 / mrr@6 / recall@6；召回缺口 = top-50 无 relevant 块，排序缺口 = top-50 有但 top-6 不进）：

| run | hit@6 | mrr@6 | recall@6 | 召回缺口 | 排序缺口 |
|---|---|---|---|---|---|
| G1 chunk BM25 | 41/49 = 83.7% | 0.6473 | 0.0801 | 4 | 4 |
| G2 dense BGE-M3 | 43/49 = 87.8% | 0.6540 | 0.0679 | **0** | 6 |
| G3 hybrid RRF | 40/49 = 81.6% | 0.6833 | 0.0747 | **0** | 9 |

结论（支撑 roadmap §4 门禁修订为两层）：
- **召回层达成（4→0）**：BM25 的 4 个召回盲区（S1-02/S1-16/S1-17/S2-14，均为语义问法、零词面重叠）全部被向量补上；其中 S1-02/S2-14 被 BGE-M3 dense 直接救进 top-6，S1-16/S1-17 进 50 候选但 top-6 不进（Phase 2 rerank 靶子）。"该语料用得上向量"实锤，旧停级条款不触发。
- **RRF 融合负贡献**：dense 单独救回的 S1-02/S1-23/S2-04/S2-14 一过 RRF 融合全被挤出 top-6，融合仅额外救回 S1-20 → G3 hit@6 40 < G2 43。RRF 的 top-50→top-6 一步在注入窗口上是净亏；RRF 定位收回为"召回面兜底（union 候选池）"，精排归 Phase 2 rerank（roadmap Phase 2 原设计即 rerank top-50→top-6）。
- **词面/语义互补性双向证实**：S1-07/S1-25（词面题）BM25 中、dense 不中；语义题反之 → 两通道召回面互不覆盖，混合必要。
- **needle 无提升空间**：去重后 needle 9/9 全通道全对，旧门禁"needle/cross 语义受益 +15pp"假设不成立；cross_doc 提升来自 dense（13→15/15），G3 融合又掉回 13。
- **旧门禁数学不可能**："needle+cross 25 题 +15pp" 每题 4pp、全对仅 +12pp，物理达不到——已修订为分层门禁（roadmap §4）。
- 新增评测工具：`rag-bench/scripts/report_layer_gates.py`（分层门禁报告）、`gen_answer_chunk_annotations.py`（答案块标注工作文件，qrels 收细用，待人工复核）。
- **题源配比缺口（3c，诚实记录）**：59 唯一题中 `semantic=True` 仅 10 题（~20%，全部在 single_doc；cross/needle 均为词面直问）。这是构造分布而非实测分布——真实用户问法的词面/语义占比无样本可得（workspace 无会话数据）。语义占比越高，"向量必要"的命中率证据越强；当前 20% 是下限猜测，需上线后采样校准（这是"向量收益面"最终定价的数据缺口）。

**口语集（30 题 Fuzzy）与端到端混合验收（2026-09-06 凌晨，embedding=qwen3-embedding-8b 口径，`questions-fuzzy.jsonl` / `results/l1-fuzzy/` / `results/crag-detail-v2-g3-hybrid.json`）**：

针对上节 3c 的配比缺口，构造 30 条纯口语/模糊指代问题（cross_doc 15 / needle 10 / single_doc 5，故意规避文档术语），把"语义占比"推到 100% 做上界校准：

| run | hit@6 | mrr@6 | cross_doc | needle | single_doc |
|---|---|---|---|---|---|
| G1 BM25 | **0.3333** | 0.1867 | 0.4000 | 0.4000 | **0.0000** |
| G3 hybrid | **0.8000** | 0.4900 | 0.9333 | 0.6000 | 0.8000 |

- **BM25 在口语分布上崩塌**（术语集 0.82 → 口语集 0.33，single_doc 全灭）：同一系统同一语料，只换查询分布——"向量增益被术语集天花板锁死"实锤，+46.7pp 是向量通道价值的上界证据（与 BGE-M3 存档口径不同但结论方向一致：召回缺口 4→0 的互补面）。
- needle 口语 0.60：模糊指代对 dense 也难，Phase 2 rerank 的量化靶子。

端到端混合验收（`COUNTBOT_RAG_HYBRID=1`，60 题 v2 judge + pairwise 对照 G1 基线）：

| 指标 | G1 基线 | G3 混合 | 判定 |
|---|---|---|---|
| 负样本拒答 | 10/10 | 8/10 | ⚠️ 2 题"漏拒"实为 **judge 故障假象**（S4-06/08 verdict=unknown=解析失败回退关键词；答案本身是合格的"知识库没有提及"式软拒答）→ 实质未回退 |
| 正样本误拒答 | 10/50 | 10/50 | 持平但**结构剧变**：救回 5 题（S1-02/05/17/20/23，其中 S1-17/20 refused→correct）；新增 5 题（S1-14、S2-06/08/10、S3-04，全部 retrieval_hit=Y 的 grader_error） |
| verdict 分布 | correct 22 / partial 13 | correct 29 / partial 8 | 质量分布实质改善 |
| 引用率 | 72% | 74% | 未达 85% 门禁 |
| 系统调用/题 | 2.25 | 2.13 | 微降 |
| pairwise | — | win 18 / loss 19 / tie 21 | 持平（loss 集中在 needle） |

**核心结论（两条独立证据链指向同一决策）**：向量召回把更多"看似相关"的块喂给为 BM25 时代设计的 grader，误判面 5→9——检索层召回收益（缺口 4→0）被编排层误判放大所抵消，端到端不净赚。与上节"RRF top-6 净亏 → 精排归 rerank"的检索层结论互为印证：**Phase 2（rerank 精排 + grader 职责收缩）从优化项升级为混合检索上生产的前置条件；`COUNTBOT_RAG_HYBRID` 在此之前保持默认关闭**。

---

## 6. 测试与质量保障

- **单元/回归测试**：`tests/rag/` 35 项（分块器单测 + 开关回归：flag-off 输出与文档级逐项一致、import 投毒存活、单文档重建语义）；上游全量测试绿；
- **降级安全**：三层回落链（reranker 失败→RRF 排序；grader 失败→直出；无 embedding 配置→纯 BM25），任何一环无配置时行为逐级回落到已验证形态；
- **复现入口**：`rag-bench/scripts/` fetch_corpus → run_g0 / run_g1（程序化）/ run_crag_eval（端到端）/ l1_eval（ranx）；口径唯一来源 `eval_config.yaml`。

---

## 7. 踩坑与经验（诚实清单）

1. **先量化痛点再动手**：12,334 tokens 这个数字让"要不要分块"从观点变成事实，也是上游 issue 的核心论据；
2. **评测先行，否则是 demo 不是系统**：没有 60 题集，M2"向量有没有增量"就无法回答，只能靠信仰；
3. **把回退写进报告**：G1 的 4 处单项回退是 M2/Phase 3 的立项依据，藏起来 = 丢掉下一代优化动机；
4. **"不做"要论证**：GraphRAG/LangGraph/向量库/前置路由，每个"不做"都有数据或架构论证（§3），面试中这比"做了什么"更能区分工程判断力；
5. **数据集是开发者自己写的有构造偏差**：已识别，对策是用 LLM 反向生成 + 人工校验补充外部视角题（计划中）；
6. **上游协作**：diff 纪律（83 文件 2.3 万行的分支 → 干净的 700–1,600 行 PR）、上游无 CI 所以测试证据是唯一背书、自包含可复现（维护者可能不跑任何命令）。

---

## 8. 面试叙事索引

- **1 分钟版**：见 §0 电梯陈述，重点讲"评测先行 + 零依赖 + 拒答 100%"三个差异点；
- **"你怎么评估 RAG 效果？"** → §4 全篇（这是最高频也最能拉开差距的问题）；
- **"为什么不用 LangChain/向量库/GraphRAG？"** → §3 D3/D6/D7；
- **"遇到最难的问题？"** → 负样本 top-1 分数重叠 7/10（§2.2），从数字出发否定方案再设计的完整过程；
- **"上线后怎么保证不回退？"** → 门禁 + 停级纪律 + 单变量原则（§3 末）+ flag 默认关闭零破坏；
- **数字怎么来的 / 能复现吗？** → §5 + §6 复现入口，任何数字可溯源到 results/ 文件。

---

## 附：文档地图

| 文档 | 用途 |
|---|---|
| `docs/rag/README.md` | 文档入口（职责速查：先读哪份/什么时候读哪份） |
| `docs/rag/optimization-roadmap.md` | 战略与选型论证（M2 后总纲） |
| `docs/rag/implementation-plan.md` | 开发规格（文件/签名/门禁/测试） |
| `docs/rag/pr-submission-handbook.md` | 上游 PR 提交手册 |
| `docs/rag/archive/*` | 早期过程资产（test-plan / eval-datasets / milestones / interview-narrative 等） |
| `rag-bench/` | 评测集 + 脚本 + 结果（单一事实来源） |
| 本文 | 工程日志（社招叙述底稿，不进 PR） |
