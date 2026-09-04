# CountBot RAG 增强 · 开发文档索引

> 本目录是 **`feature/rag-enhancement`** 分支的 RAG 开发文档集。
> 分支基于 `main`，目标：把 CountBot 的词法检索（Wiki BM25 整篇召回、memory 子串搜索、news 关键词过滤）升级为**可插拔的混合 / 语义检索**，作为可扩展模块集成，零破坏现有功能。

## 文档导航

| 文件 | 用途 | 何时读 |
|------|------|--------|
| **`milestones.md`** | **里程碑开发计划 + 验收标准（M0–M5）** | 首先读这个——知道做什么、每步做到什么程度算过 |
| `architecture.md` | 市场调研驱动的架构设计总纲（七类成熟模式 → 四种可插拔模式、模块结构、不做清单） | 想了解"为什么这么做、整体怎么落地" |
| `test-plan.md` | 可量化测试方案（三层金字塔、C1–C3 语料、60 题集、G0–G3 对照、P1–P4 门禁、token 量化） | 想知道"怎么证明做对了、必要性/可行性怎么测" |
| `eval-datasets.md` | **公开数据集与工具选型**（中文基准 C-MTEB / DuReader / CRUD-RAG、2026 新增资源、工具链、按里程碑组合、坑与不做） | 想知道"现成数据集能借什么、怎么用" |
| `issue-proposal.md` | 提给开源作者的 issue 底稿（含附录 A：20 题 golden set） | 准备提 issue / PR 时照抄 |
| `interview-narrative.md` | 面试讲述框架（必要性 / 场景 / 路径 / 反问话术） | 准备面试复述时 |
| `archive/` | 早期方案（已取代/合并，冻结不再更新）：`rag-enhancement-plan.md`、`rag-control-guide.md` | 回溯选型表 / chunk schema / 风险清单时 |

## 阅读顺序建议

1. `milestones.md` —— 看里程碑划分与每阶段验收门禁；
2. `architecture.md` —— 看设计如何由市场调研倒推、四种模式如何插拔；
3. `test-plan.md` —— 看如何用数据验收（必要性 + 可行性）；
4. `eval-datasets.md` —— 看现成数据集能借什么（中文基准选型、工具链、按里程碑组合）；
5. 准备贡献时 → `issue-proposal.md` 为底板，附 `test-plan.md` 的基线数据。

> 分工：`test-plan.md` 管"自建怎么测"，`eval-datasets.md` 管"现成的能借什么"。互相不可替代。

## 开发约定（速查）

- 模块落地：`backend/modules/rag/`（摄取管线 + 四种 Retriever + 三个插槽）
- 语料注册：`workspace/rag/corpora.yaml`
- 测试落地：`tests/rag/`（golden set + 对比脚本 + 实验报告）
- 每里程碑 = 1 个 PR；提交前缀 `feat(rag)` / `test(rag)` / `docs(rag)`
- 硬约束：`AgentLoop` / `ToolRegistry` / `Cron` 零改动；现有 `tests/` 全绿

## 协作状态

- 分支：`feature/rag-enhancement`（基于 `main` @ `62486dd`）
- 当前进度：文档与里程碑规划已就绪（本提交）；代码实现从 **M0（基线）+ M1（分块）** 起步。
- 范围纪律见 `architecture.md §4` 与 `milestones.md §4`（明确"不做什么"）。
