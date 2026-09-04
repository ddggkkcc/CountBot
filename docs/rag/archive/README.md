# RAG 文档归档

> 本目录存放已被取代 / 已合并的 RAG 相关文档，**冻结不再更新**。
> 活跃文档在 `docs/rag/`：`architecture.md`（设计总纲）、`test-plan.md`（可量化测试方案）、`issue-proposal.md`（issue 底稿）、`milestones.md`（里程碑与验收标准）、`README.md`（本目录索引）。

## 归档清单

| 文件 | 归档原因 | 独有价值（如需回溯） |
|------|---------|---------------------|
| `rag-enhancement-plan.md` | 现状审计与实施路线已被 `../architecture.md`（§3.5 P1–P6、§3.3 模块结构）覆盖 | ① 技术选型对比表（嵌入模型：云端 small / 本地 bge-m3·m3e / 可切换；向量库：numpy / faiss-cpu / Chroma·Qdrant）② chunk schema 草案（`chunk_id / doc_id / heading_path / tokens / mtime`）③ 风险清单（语料接入范围、中文 embedding 质量、`mtime+hash` 增量一致性、云端嵌入成本、与 IMA 知识库的边界、长文档切分策略） |
| `rag-control-guide.md` | 方法论（golden set + 对比实验 + 阶段门禁）已升级并入 `../test-plan.md`（三层测试金字塔 + P1–P4 可测门禁） | ① 对比实验记录格式（实验卡片原型）② 指标 → 优化方向映射表（token 高→调分块 / 命中低→调 RRF 系数 / 不可溯源→强制引用模板）③ "没经验怎么把控"的面试叙事（承认短板 + 用方法补齐 + 有"何时该停"的判断） |

## 说明

- 归档 ≠ 删除：信息保留，仅移出活跃维护面。
- 若后续实施进入 Phase 0 决策（选嵌入模型 / 向量库），先读 `rag-enhancement-plan.md` 的选型表再定。
- `issue-proposal.md` 为 issue 底稿（活跃文档）；提交 issue 后可归档到此处（属工作文件，不强制保留）。
