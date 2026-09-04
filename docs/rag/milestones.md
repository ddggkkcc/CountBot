# CountBot RAG 增强 · 里程碑开发计划与验收标准

> 分支：`feature/rag-enhancement`（基于 `main` @ `62486dd`）
> 配套文档：`architecture.md`（市场调研驱动的设计总纲）、`test-plan.md`（可量化测试方案）、`issue-proposal.md`（issue 底稿）、`interview-narrative.md`（面试讲述）、`archive/`（早期方案）
> 目标：把 CountBot 现有的"词法检索"（Wiki BM25 整篇召回、memory 行式子串搜索、news 关键词过滤）升级为**可插拔的混合 / 语义检索**，作为可扩展模块集成，且**零破坏**现有功能。
> 核心原则：① 市场调研驱动选型（见 `architecture.md §1`）；② 证据驱动落地，每步有可量化门禁（见 `test-plan.md`）；③ **阶段门禁不过即回退 / 记录，"停在某一级也是有效结论"**。

---

## 0. 分支与协作约定

- **模块落地目录**：`backend/modules/rag/`
- **文档落地目录**：`docs/rag/`（本目录）
- **语料注册表**：`workspace/rag/corpora.yaml`
- **测试落地目录**：`tests/rag/`（golden set + 对比脚本 + 实验报告）
- **PR 策略**：每个里程碑 = 1 个独立 PR，可独立 review / merge，不一次性合入全部。
- **提交前缀**：`feat(rag)` / `test(rag)` / `docs(rag)`。
- **硬约束**：每次 PR 现有 `tests/` 必全绿；`AgentLoop` / `ToolRegistry` / `Cron` **零改动**。

---

## 1. 里程碑总览

| 里程碑 | 设计阶段 | 核心交付 | 验收门禁（摘要，详细见 §2） |
|--------|---------|---------|---------------------------|
| **M0** 基线与测试台 | — | 语料 C1–C3 + 60 题集 + 中文公开基准 + G0 基线 | 基线数据可复现（token / 溯源率 / 命中率）；基准版本锁定 |
| **M1** 分块 + BM25 分块级 | P1 | `chunker.py` + 分块 BM25 索引 + Wiki 委托 | 上下文 token −70%；召回为块；可溯源 ≥70%；关键词场景未劣化 |
| **M2** 向量 + 混合 + 回归 | P2 | `embeddings.py` + 向量库 + RRF + `ranx` 指标 + golden 回归 | 语义变体命中 +≥3；L2 四指标 + 接地率 ≥0.90；T2Retrieval NDCG@10 ≥70；增量索引；降级可用 |
| **M3** 语义记忆 | P3 | memory 嵌入钩子 + SemanticRecency | 跨会话变体召回 ≥2；衰减生效；误召回低 |
| **M4** 时序聚合 | P4 | news_pool + TemporalDedup | 跨源去重；个性化排序；来源权重生效 |
| **M5** 生成辅助 + 重排（可选） | P5/P6 | 范例库 + 注入模板 / bge-reranker | 生成 token 下降 / top50→top5 精度提升 |

> 递进关系：M0 是"before"地基；M1 零新依赖先解上下文爆炸；M2 补齐 hybrid 另一半并用 golden set 验证增量；M3/M4 逐模式接入；M5 后置，不与前序绑定。

---

## 2. 各里程碑详述

### M0 · 基线与测试台（必要性的地基）

- **目标**：建立"before"，使后续每一步改动都能量化对照；同时正面回答"现状有多痛"。
- **交付**：
  - `tests/rag/corpora/`：抓取官方文档站全量 → **C1**（~20–40K token）、扩展 **C2**（~50–80K）、压力 **C3**（≥200K，用公开来源填充，保证可复现）。
  - `tests/rag/questions.json`：**60 题**（S1 单文档 25 / S2 跨文档 15 / S3 压力 10 / S4 负样本 10），每题含 `expected_source`。
  - `tests/rag/baseline_g0.md`：现状（BM25 整篇 top-3 灌上下文）跑 G0 的指标记录。
  - `tests/rag/benchmarks/`：中文公开基准就位 —— `C-MTEB` 三个检索子集（T2Retrieval / DuRetrieval / EcomRetrieval）、DuReader-Retrieval 三档抽样（10 万 / 50 万 / 200 万段落）、CRUD-RAG QA 子集（1-doc / 2-doc / 3-doc）；**记录 commit / 版本号与抽样 seed**。
- **验收**：
  1. 语料仅用官方文档 + 仓库通用文档（作者可复现，不掺个人私有语料）；
  2. G0 记录完整：上下文 token 中位数、可溯源率（应为 0%）、golden 要点命中率、C3 截断率；
  3. 固定 seed；若用 LLM judge 则固定模型与版本；
  4. 公开基准数据就位**且版本固定**（数据集更新会导致跨版本分数不可比），抽样 seed 已记录。

> **为什么中文公开基准必须在 M0 就位**：CountBot 走 jieba 分词路径，英文基准（BEIR / MS MARCO / HotpotQA）无法复现其行为，只借方法论、不跑语料。中文基准是 M1/M2 门禁的硬依赖，必须在基线阶段锁定版本，否则后续改动无法在同一版本上做 before/after。选型依据见 `eval-datasets.md`。
- **退出条件**：M1 可在同一批题上做 before/after 对比。未达标则 M0 本身即阻塞（无基线不推进）。

### M1 · 分块 + BM25 分块级召回（P1）

- **目标**：先解决"整篇召回打爆上下文"，**零新依赖**，立刻见效。
- **交付**：`backend/modules/rag/chunker.py`（`HeadingChunker`）、`stores/bm25_store.py`（分块级，复用 `wiki/index.py` 分词）、`wiki/tool.py` 的 `ask`/`search` 委托 RAG 层。
- **验收（全部可测）**：
  1. 同题上下文 token 下降 **≥70%**（C2 实测）；
  2. 召回的是**相关块**而非整篇；
  3. golden set **≥70%** 答案可溯源到具体块（`[slug#section]`）；
  4. 现有 `tests/` 全绿、`AgentLoop` 零改动；
  5. **关键词场景未劣化**：`C-MTEB/EcomRetrieval` 上 BM25 不低于现状基线 —— 确认分块没有砍掉短句精确匹配能力（对应 `test-plan.md §5` P1 门禁 ⑤）。
- **退出条件**：未达标则回退并记录原因，不进入 M2。

### M2 · 向量 + 混合 + golden 回归（P2）

- **目标**：补齐 hybrid 的另一半（向量），用 golden set 验证"向量是否带来增量"。
- **交付**：`embeddings.py`（LiteLLM / Ollama / Noop 降级）、`stores/vector_store.py`（numpy / faiss）、`retriever.py` 的 `HybridRRF`、评估脚本、**`ranx` 指标集成**、DuReader 规模衰减实验脚本。
- **验收**：
  1. S1 语义变体题命中 **≥ G1 且新增命中 ≥3 题**；
  2. **若向量无提升 → "该语料用不上向量"为有效结论，停在此级（写入 PR 说明），不强行上向量**；
  3. L2 四指标达生产目标：`context precision ≥0.70`、`context recall ≥0.85`、`faithfulness ≥0.85`（RAGAS 定义）；
  4. **逐段落接地率 ≥0.90** —— 抓 RAGAS 四指标漏掉的静默失败：答案看似正确、要点也命中，但支撑它的检索块是错的（模型凭参数知识补全）；
  5. **中文公开基准达量级**：`C-MTEB/T2Retrieval` NDCG@10 **≥70**（开源基线 ≈85，本项目门槛定 70，见 `test-plan.md §4.1.1`）；
  6. **规模衰减曲线产出**：DuReader-Retrieval 三档（10 万 / 50 万 / 200 万段落）recall@50 数据 —— 自建 C3 只有单一规模点，此曲线是"语料增长后检索必然劣化"的独立外部佐证；
  7. 增量索引：改一篇只重算该篇（`mtime` + `content_hash` 双校验）+ **CRUD-RAG 幻觉修改任务通过率不下降**（语料更新后的天然回归测试：增量索引不生效则此项分数下滑）；
  8. 嵌入源不可用自动回落 BM25，检索不中断。
- **退出条件**：门禁全过，或触发"停级"分支（均为合格产出）。

> **嵌入模型选型**：先试 `bge-small-zh-v1.5`，而非 CRUD-RAG 默认的 `bge-base-zh-v1.5`。单机个人运行时，若 small 与 base 在 T2Retrieval 上 NDCG@10 差距 **<3 分，即用 small，并把这个对比本身写进 PR**——有取舍依据的工程判断比堆参数更能加分。

### M3 · 语义记忆（P3，Mode B）

- **目标**：memory 从子串搜索升级为语义召回（个人上下文画像）。
- **交付**：`agent/memory.py` 的 `append` 异步嵌入钩子、`SemanticRecency` retriever（时间衰减 `0.5^(Δdays/30)`，类型分 `preference` 不衰减 / `fact` / `event` 快衰减）。
- **验收**：
  1. 跨会话召回"字面不同、语义相关"偏好 **≥2 例**（如"用 zsh" ↔ "我的开发环境偏好"）；
  2. 近期记忆优先（时间衰减生效）；
  3. 误召回率低（S4 负样本通过）。
- **退出条件**：未达标则记录"记忆语义化收益不足"，回退子串搜索（行为不变）。

### M4 · 时序聚合（P4，Mode C）

- **目标**：news 从关键词过滤升级为"时间窗 + 语义去重 + 来源可靠性加权"。
- **交付**：`skills/news/` 拉取写 `news_pool`（`URL hash` 幂等）、`TemporalDedup` retriever（cos>0.85 聚簇、官方 3×/媒体 2×/社区 1× 加权、滚动淘汰 90 天）。
- **验收**：
  1. 跨源重复被合并（同簇多源合并叙事并注明多源验证）；
  2. 个性化排序命中偏好；
  3. 来源可靠性权重生效。
- **退出条件**：未达标则保留 `--keyword` 粗筛，不替换。

### M5 · 生成辅助 + 重排（可选，P5/P6）

- **目标**：生成端范例检索（web-design 等）+ 重排精排。
- **交付**：范例库 + `ExemplarTopK`、可选 `bge-reranker`（top50→top5）。
- **验收**：生成 token 注入下降 / top50→top5 精度提升；**无提升则不接**。
- **退出条件**：后置，不与 M1–M4 绑定；可作为独立后续 issue。

---

## 3. 验收方法（统一口径）

- **三层测试金字塔**（`test-plan.md §2`）：
  - L1 检索组件：`NDCG@10` / `recall@50` / `MRR@10`，**程序化打分（`ranx`），最可复现**（不依赖 LLM judge）；语料 = 中文公开基准（C-MTEB 检索子集 + DuReader 规模衰减）+ 自建 C1–C3；
  - L2 端到端：RAGAS 四指标 + golden 要点命中率 + **逐段落接地率**，LLM-as-judge 固定模型版本、≥20% 样本人工校准、答案顺序交换取均值防 position bias；外部验证集用 CRUD-RAG QA 子集（**只取数据与标注，不用其 Milvus harness**）；
  - L3 系统对照：token 压缩比（基线 ÷ RAG）、货币化成本、正确率、截断率（`test-plan.md §4.3`）。
- **单变量原则**：一次只改一处，before/after 各跑同一批题（`test-plan.md §8`）。
- **实验报告模板**：`test-plan.md §6`（改动 / 对照对象 / 语料规模 / judge / 指标对比 / 变好变坏 / 门禁判定）。
- **"停在此级也是结论"**：M2/M3/M4/M5 门禁均含"无提升即停级"分支，降低作者采纳顾虑，也防止为显得专业而堆概念。

---

## 4. 不做（范围纪律，每条有市场证据）

来自 `architecture.md §4`，每条对应生产实践的反例：

- 不上独立向量数据库服务（"vector DB first" 2026 被推翻；个人语料规模 numpy 足够）；
- 不把检索硬编码进 `AgentLoop`（检索正从管道变工具；硬编码把 Agent 退化成管道）；
- 不推倒 BM25 重来（hybrid 是生产默认形态，BM25 是合法一半；Claude Code 甚至纯词法）；
- 不做 GraphRAG（多跳 +27 分但通用 QA 仅 +0.47 分，本项目无多跳需求）；
- 不引入 LangChain / LlamaIndex 重框架（解决企业通用问题，单机个人运行时引入 = 生态税）；
- 不追"向量库都不要"的营销叙事（"vectorless RAG" 无可复现结果，仅代码场景词法占优）；
- 不拿个人私有语料（面经 / 学习笔记）做公开论证（issue/PR 只用官方文档语料）。

---

## 5. 提 issue / PR 的衔接

- **先提 issue**：以 `issue-proposal.md` 为底板，附 M0 基线数据（必要性从"观点"变"事实"）。
- **每个里程碑一个 PR**：描述含改动、对照对象、语料规模、门禁结论、现有测试状态。
- **作者可复现**：语料 = 官方文档站 + 仓库通用文档；60 题全部标注出处；judge 模型版本固定。
- **降低采纳成本**：零破坏（向量池是派生索引，删后可重建）、可拆多 PR、每阶段有门禁与"停级"分支。

---

## 附：现状代码锚点（M1 起步参考）

- Wiki 整篇召回：`backend/modules/wiki/tool.py`（`ask` 走 `search(top_k=3)` → 整篇 `content` 拼 prompt）
- BM25 实现：`backend/modules/wiki/index.py`（jieba 分词、标题/标签加权、无向量/分块/重排、`mtime` 增量）
- 记忆子串搜索：`backend/modules/agent/memory.py`（`search` 为关键词匹配）
- 工具注册：`backend/modules/tools/registry.py`（`RagTool` 将注册进此表，`AgentLoop` 零改动）
