# CountBot RAG 增强：公开数据集与工具选型

> 状态：v1（2026-08-29），配套文档：`test-plan.md`（测试方案主体）、`milestones.md`（里程碑与门禁）、`architecture.md`（设计总纲）
>
> 目的：回答 `test-plan.md` 未覆盖的问题——**现成数据集有哪些、哪些真能用、怎么接进来**。
>
> 定位：本文档是 `test-plan.md §1 市场调研` 的下游展开。`test-plan.md` 负责"自建怎么测"，本文档负责"现成的能借什么"。**两者不互相替代**：公开基准用于横向可比性，自建题集用于领域相关性。

---

## 1. 三条选型判断（先给结论）

1. **中文语料的检索组件必须用中文基准验证，英文基准只借方法论。**
   CountBot 的 `wiki/index.py` 走 jieba 分词 + 标题/标签加权，这条路径的行为在 BEIR / HotpotQA 上无法复现。英文基准的价值只在于方法论（NDCG@10 主指标、oracle vs achieved 归因、四类失败模式分类），**不跑它的语料**。

2. **L3 系统对照层没有现成基准，必须自建——这是 CountBot 真正的差异化证据。**
   公开基准测的是"通用 QA 能力"，而本项目的核心论点是"持续运行的 Agent 不能靠大窗口塞全文"。token 压缩比、可溯源率、context rot 衰减，这些指标没有任何公开数据集能给出，**只能自建**（`test-plan.md §4.3` 已设计）。

3. **M3/M4（语义记忆、时序聚合）没有任何公开基准覆盖，不要浪费时间找。**
   公开 RAG 基准默认范式是"静态文档库 + 单轮问答"。CountBot 的 memory 是跨会话的用户画像，news 是带时间衰减的多源流——**二者都是非静态语料**，唯一可行的验证是自建场景脚本。这是范围纪律，不是偷懒。

---

## 2. 中文数据集（主力，重点投入）

### 2.1 C-MTEB 检索子集 —— L1 检索组件的横向标尺

**为什么选它**：这是目前最完整的中文语义向量评测基准，35 个公开数据集、6 类任务，评测协议直接沿用 BEIR，**NDCG@10 为主指标**——与 `test-plan.md §4.1` 的 L1 指标口径完全一致，无需转换。

**可直接用于检索评测的 8 个子集**（HuggingFace `C-MTEB/*`）：

| 数据集 | 领域 | 测试样本 | 对 CountBot 的用途 |
|--------|------|---------|-------------------|
| `C-MTEB/T2Retrieval` | 通用段落排序 | 24,832 | **主力**，规模最大，query-to-passage |
| `C-MTEB/MMarcoRetrieval` | 多语言 MS MARCO | 7,437 | 中文翻译语料，测跨语言表述 |
| `C-MTEB/DuRetrieval` | 真实搜索引擎 | 4,000 | query 来自百度真实用户提问，**最接近真实查询分布** |
| `C-MTEB/CmedqaRetrieval` | 医疗问诊 | 3,999 | 领域外测试，验证泛化 |
| `C-MTEB/CovidRetrieval` | 疫情新闻 | 949 | 小规模快速回归 |
| `C-MTEB/EcomRetrieval` | 电商搜索 | 1,000 | 句级关键词匹配，**BM25 应占优**，用于确认"哪些场景确实不需要向量" |
| `C-MTEB/MedicalRetrieval` | 医疗 | 1,000 | 同上 |
| `C-MTEB/VideoRetrieval` | 视频 | 1,000 | 同上 |

**重排子集**（M5 阶段验证 reranker 时用）：`T2Reranking`(24,382)、`MMarcoReranking`(7,437)、`CMedQAv1`(2,000)、`CMedQAv2`(4,000)，主指标 MAP。

**参考基线**（C-MTEB 已发表结果，NDCG@10）：BGE-large 在 T2Retrieval 84.82 / MMarcoRetrieval 81.28 / DuRetrieval 86.94；OpenAI-Ada-002 为 69.14 / 69.86 / 71.17。**这组数字的价值在于给出"好"的量级**——如果自研混合检索在 T2Retrieval 上到不了 70，说明分块或嵌入选型有问题，而不是指标标准定高了。

**接法**：不要跑全套 35 个数据集。只取 `T2Retrieval` + `DuRetrieval` + `EcomRetrieval` 三个，覆盖"通用 / 真实查询分布 / 关键词主导"三种形态，前两个验证向量增量，第三个验证 BM25 保留价值。

### 2.2 DuReader-Retrieval —— 大规模中文段落检索压测

**为什么选它**：C-MTEB 语料规模偏小（最大 24K 查询、段落库几十万级），而 CountBot 的 C3 压力语料目标是 ≥200K token。DuReader-Retrieval 提供 **dev 集 2,000 条查询 + 809 万段落**，是检验"语料规模增长时检索是否劣化"的最佳现成语料。

**指标口径**：MRR@10、Recall@1、Recall@50（与 C-MTEB 的 NDCG@10 互补，Recall@50 专门看深层召回能力——正好对应 `test-plan.md` 的 S3 压力题）。

**接法**：截取其中 10 万 / 50 万 / 200 万段落三个规模档，跑同一批 500 条查询，观察 recall@50 的衰减曲线。**这条曲线就是"语料变大后直读为什么不行"的外部佐证**，比只在自建 C3 上测更有说服力。

### 2.3 CRUD-RAG —— 端到端中文 RAG 行为

**为什么选它**：这是规模最大的中文 RAG 测试集（**36,166 样本**），且覆盖了 QA 之外的任务形态。它的 CRUD 分类对 CountBot 有直接映射价值：

| CRUD 任务 | 样本数 | 对应 CountBot 场景 | 覆盖能力 |
|-----------|-------|-------------------|---------|
| QA（1 文档） | 3,199 | wiki 单页问答 | Read → 基础检索 |
| QA（2 文档） | 3,192 | **跨文档整合（S2 题）** | Read → 信息整合 |
| QA（3 文档） | 3,189 | 复杂跨页推理 | Read → 多跳 |
| 幻觉修改 | 5,130 | 语料更新后的纠错 | Update → **CountBot 特有**：wiki 内容会随文档更新 |
| 多文档摘要 | 10,728 | news 聚合（M4） | Delete → 压缩 |
| 文本续写 | 10,728 | 生成辅助（M5） | Create |
| 检索库 | 86,834 篇新闻 | — | 语料基础 |

**关键洞察**：CRUD 分类里 **Update（幻觉修改）是绝大多数基准没有的**，而这恰恰是 CountBot 的真实痛点——官方文档会升级（v0.9 → 新版本），旧知识块必须被纠正。这一项可以直接支撑 `milestones.md` M2 验收里的"增量索引：改一篇只重算该篇"——**如果增量索引不生效，幻觉修改任务的分数会明显下降**，这是一个天然的回归测试。

**⚠️ 接法要点（重要）**：
- **不要直接用它的 harness**。CRUD-RAG 官方实现依赖 Milvus-lite 向量库 + bge-base-zh-v1.5，首次建索引约 3 小时。而 `milestones.md §4` 明确"不上独立向量数据库服务，numpy 足够"——**架构冲突**。
- **正确用法**：只取 `data/crud_split/split_merged.json` 的题目 + `data/80000_docs/` 的文档，套到自研 Retriever 上跑。它的价值是**语料 + 标注**，不是评测框架。
- 指标用 ROUGE / BLEU / BERTScore 即可（自研 judge 成本可控）；RAGQuestEval 需要额外 LLM 调用，M2 阶段先跳过。

### 2.4 2026 年新增资源（按需取用）

| 资源 | 规模 | 特点 | 用不用 |
|------|------|------|--------|
| **RAGEval**（OpenBMB） | 按 schema 从种子文档自动生成 | 机器-人工打分绝对差 **1.67%**，标注者间 Fleiss' Kappa **0.7686** | **用**：自建题集扩量时的合成工具，可靠性有量化背书 |
| **GaRAGe** | 逐段落 `evidence_relevant` / `evidence_correct` 标注 | 能抓"答案看起来对但接地错误"的静默失败 | **用**：M2 门禁补一条"逐段落接地率"，这是 RAGAS 四指标抓不到的 |
| **MTRAG** | 110 段人工对话，平均 7.7 轮，842 tasks | 多轮会话 RAG | **部分用**：CountBot 是 Agent，多轮是常态，但语料为英文——**借它的多轮评测协议，题目自建** |
| **TechQA-RAG-Eval** | ~908 QA pairs，带 `is_impossible` 标志 | 技术问答领域，含不可回答问题 | **用标注思路**：`is_impossible` 正是 `test-plan.md` S4 负样本集的标准做法 |
| **LIT-RAGBench** | 114 题，5 类（Integration/Reasoning/Logic/Table/Abstention） | 用虚构实体构建，**杜绝训练数据污染** | **不用**：日/英语；但"用虚构实体抗污染"的思路值得抄进自建题集——例如构造一个虚构的 CountBot 配置项，验证模型不会凭参数知识编造 |
| **EnterpriseRAG-Bench** | >50 万文档，9 类来源 | 企业异质语料 + 噪声注入 | **不用**：规模远超个人运行时，与 `architecture.md §4` 的范围纪律冲突 |

---

## 3. 英文数据集（只借方法论，不跑语料）

`test-plan.md §1.1` 已列出的 BEIR / MS MARCO / HotpotQA / MultiHopRAG / FRAMES / RGB / RULER **维持原判断**，此处只补充"借什么"：

| 资源 | 借的东西 | 不借的东西 |
|------|---------|-----------|
| BEIR | NDCG@10 / MRR / recall@k 的**评测协议**与 9 类检索任务划分 | 18 个英文数据集 |
| FRAMES | **oracle vs achieved 差值**归因法（824 题，每题需整合 2–15 篇）——已在 `test-plan.md §4.3` 采用 | 英文 Wikipedia 语料 |
| RGB | **四类失败模式分类模板**（噪声鲁棒 / 负样本拒答 / 信息整合 / 反事实鲁棒）——已映射为 S1–S4 | 英文问题集 |
| RULER | **"有效窗口 < 标称窗口"的量化结论** + 针埋大海的程序化打分法 | 13 个合成任务 |
| HotpotQA / MuSiQue / 2WikiMQA | 多跳题目构造范式 | 语料 |

**判断依据**：CountBot 是中文个人 Agent 运行时。跑英文基准得到的分数既不能证明中文场景有效，也无法在 issue 里说服作者（对方会直接质疑语料无关性）。**把跑英文基准的算力花在扩 C3 语料规模上，收益更高。**

---

## 4. 工具链（可执行）

```bash
# L1 检索指标：ranx 支持 recall/MRR/NDCG/MAP，且内置 RRF —— 直接对应 G2 混合检索
pip install ranx

# L1 补充：trec_eval 的 Python 封装（ranx 未覆盖的指标）
pip install pytrec_eval-terrier

# L2 端到端：RAGAS 四指标
pip install ragas

# 嵌入模型评测与数据集加载
pip install mteb          # 含 C-MTEB，直接指定 C-MTEB/T2Retrieval 即可
pip install FlagEmbedding # bge 系列中文嵌入
```

**嵌入模型选型建议**（已评审通过，并入 `milestones.md` M2）：CRUD-RAG 默认用 `bge-base-zh-v1.5`，C-MTEB 上表现最好的是 `bge-large-zh`（T2Retrieval NDCG@10 ≈ 84.8）。但 CountBot 是单机个人运行时，**先试 `bge-small-zh-v1.5`**（512 维 / 12 层；base 为 768 维 / 12 层，large 为 1024 维 / 24 层）——向量索引是派生索引、可重建，用大模型的收益未必覆盖首次建索引的时间成本。在 T2Retrieval 上跑 small vs base 对比，**若 NDCG@10 差距 <3 分，即用 small，并把这个对比本身写进 PR**（有取舍依据的工程判断比堆参数更能加分）。

**量化组合拳（本项目的推荐配置，已于 2026-08-29 评审通过并落入 `milestones.md` 门禁）**：

| 层 | 工具 | 数据 | 指标 |
|----|------|------|------|
| L1 | `ranx` | C-MTEB/T2Retrieval、DuRetrieval、EcomRetrieval | NDCG@10、recall@50、MRR@10 |
| L1.5 | `ranx` | DuReader-Retrieval（三档规模） | recall@50 衰减曲线 |
| L2 | `ragas` + 自建 judge | 自建 60 题 + CRUD-RAG QA 子集 | 四指标 + 逐段落接地率（GaRAGe 思路） |
| L3 | 自建脚本 | 自建 C1–C3 | token 压缩比、可溯源率、截断率 |

---

## 5. 按里程碑的推荐组合

> **状态：已于 2026-08-29 评审通过，并同步进 `test-plan.md §5`（门禁）与 `milestones.md`（M0–M2 交付与验收）。** 本表是索引视图，**验收标准以 `milestones.md` 为准**。

| 里程碑 | 用哪些数据集 | 新增工作量 | 门禁（已生效） |
|--------|------------|-----------|---------------|
| **M0** 基线 | 下载并锁定中文公开基准版本：C-MTEB ×3、DuReader 三档抽样、CRUD-RAG QA 子集 | 低 | 基准数据就位**且版本固定**；抽样 seed 已记录 |
| **M1** 分块+BM25 | `EcomRetrieval` | 低 | 关键词场景未劣化：BM25 不低于现状基线 |
| **M2** 向量+混合 | `T2Retrieval`、`DuRetrieval`、CRUD-RAG QA（1/2/3-doc）、CRUD 幻觉修改子集 | 中 | T2Retrieval NDCG@10 ≥70；逐段落接地率 ≥0.90；DuReader 三档 recall@50 曲线；幻觉修改任务通过率不下降 |
| **M2** 增益判定 | `DuRetrieval` 单独看语义变体题 | 低 | 若向量增量 <3 分 → 停级（已有分支） |
| **M3** 语义记忆 | **无公开基准**，自建跨会话场景 | 中 | 保持现有设计 |
| **M4** 时序聚合 | CRUD-RAG 多文档摘要子集（10,728）可部分复用 | 中 | 摘要去重率可借用 ROUGE |
| **M5** 重排（可选） | C-MTEB 重排子集（T2Reranking 等） | 低 | MAP 提升 |

**刻意不做**：M0–M2 阶段不要同时引入超过 2 个新数据集。`test-plan.md §8` 的单变量原则同样适用于语料——**换语料和换算法都是变量，一次只能动一个**，否则 G1 vs G2 的对比无法归因。

---

## 6. 坑与不做

| 坑 | 说明 | 应对 |
|----|------|------|
| **CRUD-RAG 架构冲突** | 官方 harness 依赖 Milvus-lite，与"不上独立向量库"冲突 | 只取数据与标注，不取框架 |
| **CRUD-RAG 指标漂移** | 官方 README 已提示：BLEU/ROUGE 基于字符串匹配，模型输出风格从 2023 至今变化大，分数不可与论文直接比较 | **只做 before/after 自比**，不与论文数字横向比 |
| **首次建索引成本** | CRUD-RAG 8.6 万文档建索引约 3 小时；DuReader 809 万段落更长 | 抽样使用（取 1–5 万文档子集），记录子集划分 seed |
| **污染问题** | 公开基准可能被模型训练数据覆盖，导致分数虚高 | 抄 LIT-RAGBench 思路：自建题集中掺入虚构实体（如虚构配置项）验证抗污染 |
| **英文基准的伪精确** | 跑英文基准能拿到漂亮数字，但与中文场景无关 | 明确不跑（§3） |
| **MTEB 版本号** | MTEB/C-MTEB 数据集版本会更新，跨版本分数不可比 | 实验卡片记录数据集 commit/版本号（与 judge 版本固定同理） |

**不做**：
- 不跑 C-MTEB 全套 35 个数据集（只取检索/重排子集）；
- 不接 EnterpriseRAG-Bench（规模与架构纪律冲突）；
- 不自建嵌入模型评测体系（C-MTEB 已是事实标准，重复造轮子无收益）；
- 不把公开基准分数写进 issue 作为主要论据——**issue 主论据始终是本项目的 before/after 对照**，公开基准只作为"方法可信、量级合理"的旁证。

---

## 附录：来源

- C-MTEB / C-Pack：Xiao et al., *C-Pack: Packed Resources For General Chinese Embeddings*（arXiv:2309.07597）；FlagOpen/FlagEmbedding `C_MTEB`；EvalScope C-MTEB 文档（v1.7.0）
- DuReader-Retrieval：Qiu et al.；PEG 论文（arXiv:2311.11691）实验设置
- CRUD-RAG：Lyu et al., *ACM TOIS*（10.1145/3701228）；GitHub `IAAR-Shanghai/CRUD_RAG`
- RAGEval / GaRAGe / MTRAG / TechQA-RAG-Eval：MLflow《RAG Evaluation Datasets: A Developer's Reproducible Workflow》
- LIT-RAGBench：Itai et al., LREC 2026（arXiv:2603.06198）
- EnterpriseRAG-Bench：Chhabra et al. 2026 等
