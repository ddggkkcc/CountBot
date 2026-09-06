# CountBot RAG · 文档入口（速查）

> 项目一句话：给开源 Agent 框架 CountBot 的 Wiki 知识库做 RAG 升级（issue #107 已获维护者确认，PR 进行中）——从"BM25 整篇召回"升级为"标题分块检索 + CRAG 纠偏路由（可拒答）+ 混合检索演进"。
> 本文档只告诉你**每份文档在干什么、什么时候该读哪份**，不承载实质内容。
> 更新日期：2026-09-06 ｜ 分支：`pr/rag-phase1-retrieval-baseline`

---

## 0. 现在该读哪份？（按场景速查）

| 你想做什么 | 读这份 |
|---|---|
| 一句话了解项目全貌 / 最新进展 | `work-log.md` §0 一页纸摘要 + §2 时间线 |
| 知道**为什么**做这些、**为什么不做**别的（选型论证） | `optimization-roadmap.md`（战略总纲） |
| 知道**具体怎么做**（文件 / 签名 / 门禁 / 测试） | `implementation-plan.md`（开发规格） |
| 发 PR 给上游（文案 / 检查清单） | `pr-submission-handbook.md` |
| 查某个数字 / 实验结论 | 去 `rag-bench/results/`（单一事实来源），别翻 docs |
| 用 agent 审计 main 分支代码 | `audit-prompts.md`（一次贴一个 prompt） |
| 回溯早期方案 / 术语概念 / 面试叙事 | `archive/`（已归档，仅供回溯） |

## 1. 现行文档一览（docs/rag/）

| 文档 | 一句话职责 | 状态 | 读者 |
|---|---|---|---|
| **`work-log.md`** | 工程日志：时间线 + 关键决策 + 事实底稿，含文档地图 | ✅ 持续更新（社招叙述底稿，不进 PR） | 所有人（第一站） |
| **`optimization-roadmap.md`** | 战略与选型论证：Phase 1–3 规划 + "不做什么"清单（GraphRAG/LangGraph/向量库等） | ✅ M2 后总纲 | 想理解动机的人 |
| **`implementation-plan.md`** | 开发落实规格：P0 阻塞项（`chat_completion` API 断裂）+ 分阶段实现 + 验收门禁 + 测试 | 🔄 待评审（Phase 0 阻塞中） | 要开工写代码的人 |
| **`pr-submission-handbook.md`** | PR 提交手册：审查结论 → 分支提交计划 → PR 正文 → 发前清单 | ✅ 发 PR 前用 | 要提交上游的人 |
| **`audit-prompts.md`** | agent 审计提示词（针对 **main 分支**，检查对象与 RAG 分支不同，勿混用） | ✅ 2026-09-03 | 审计 main 代码时 |

> 阅读顺序建议：`work-log.md` §0 → `optimization-roadmap.md` §0 → 按任务进 `implementation-plan.md` → 发 PR 前看 `pr-submission-handbook.md`。

## 2. 归档资产一览（archive/，已冻结，仅供回溯）

> 全部为 2026-08 ~ 09 早期过程资产，头部均带归档标记。**数字与结论请以 `rag-bench/results/` 为准**，不要引用 archive 里的旧数字。

| 文档 | 内容 | 什么场景会翻它 |
|---|---|---|
| `primer.md` | 术语解码 + 前置知识 + 阅读地图 | 忘了代号体系 / BM25 / chunk 是什么 |
| `architecture.md` | 市场调研驱动的架构设计总纲 | 追溯"当初为什么这么设计" |
| `test-plan.md` | 可量化测试方案（含未执行部分） | 回溯测试方法论 |
| `eval-datasets.md` | 公开数据集与工具选型 | 想复用外部评测集时 |
| `milestones.md` | 早期里程碑与验收标准（M2–M5 未开始） | 看历史规划 |
| `interview-narrative.md` | 面试讲述框架（个人资产） | 准备面试叙述时 |
| `open-risks.md` | "没把握清单"（风险登记） | 自查已知风险 |
| `analysis-first-principles.md` | 第一性原理诊断报告（本次重构的起因） | 理解 docs 为什么精简成现在这样 |
| `audit-prompts-rag-branch.md` | 旧版审计提示词（针对 `feature/rag-enhancement` 分支） | 已被 `audit-prompts.md` 取代，基本不再需要 |

## 3. 事实与数字去哪找（不在 docs/）

| 位置 | 内容 |
|---|---|
| `rag-bench/questions*.jsonl` | 评测集（60 题四分层） |
| `rag-bench/corpus/` | 语料 C1–C3（52 页官方文档） |
| `rag-bench/results/` | **实验报告 = 数字唯一权威**（g0-baseline / g1-chunked / crag 等） |
| `rag-bench/scripts/` | 评测脚本（l1_eval / run_crag_eval 等） |
| `memory/`（仓库根） | 跨会话项目记忆 |

> 规则：docs 里只讲结论与推理，**任何数字引用都必须能在 `rag-bench/results/` 找到出处**；发现 docs 与 results 数字不一致时，以 results 为准并修 docs。
