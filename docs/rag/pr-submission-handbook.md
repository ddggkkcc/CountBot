# PR 提交手册（v1 · Wiki 分块检索）

> 关联：issue #107（countbot-ai/CountBot，维护者已回复"欢迎提交 PR"）
> 本手册供发 PR 前使用：审查结论 → 分支与提交计划 → PR 描述正文 → 未来计划 → 发前检查清单。
> 本文件是内部工作文档，**不进 PR**。

---

## 一、审查结论（已完成）

### 1.1 通过项

| 检查项 | 结论 |
|---|---|
| 代码与 issue 描述一致 | 新模块 `backend/modules/rag/`（chunker/bm25_store/service）+ `wiki/tool.py` 开关与委托 |
| 单元测试 | `tests/rag/` 34 个 + 上游现有 1 个 = **35/35 全绿**（已在本机临时 venv 实测） |
| 开关 OFF 回归 | 专项测试：不实例化 RagService、rag 模块 import 投毒后仍正常、输出与文档级逐项一致 |
| 耦合清理 | `90e9327` gap-fix 已修：RagService 显式注入 `wiki_dir`，不再读 WikiService 私有属性；通过公共 API 访问 registry |
| 增量同步 | `on_document_added` 只重建单文档（避免全量 sync），create/update/delete 钩子齐全 |
| 上游 CI | `build-desktop.yml` 仅响应 `v*` tag / workflow_dispatch，**PR 不会触发任何自动 CI**——所以我们的测试证据是唯一背书，务必在 PR 描述中写清楚 |

### 1.2 已知权衡（评审预答弹药，不是 blocker）

1. **对 BM25Index 内部字段的直接操作**：`ChunkedBM25Index.load/save` 访问 `_inverted_index`/`_idf_cache` 等私有字段。上游 `index.py` 自身的 `save_to_file/load_from_file` 也这样做，风格一致；但若上游日后重构 BM25Index 内部结构，需要同步此适配器。缓解：模块 docstring 已注明，且全部逻辑被 34 个测试覆盖。
   - 预答："适配器复用现有实现而非重写，是为了零新依赖、零算法分叉；私有字段访问集中在这两个序列化方法内，重构风险面小，且测试会在上游改动时第一时间暴露。"
2. **BM25 绝对阈值 `SCORE_THRESHOLD=0.5` 同样作用于块级检索**：块数（1,423）远多于文档数（52），IDF 分布随之变化；实测生产路径命中率 56% → 82%，证明无碍，但语义是"与文档级一致的过滤行为"。
3. **jieba 未装时的单字分词降级在块级同样存在**：恢复 jieba 是独立的一行 PR（issue 已声明），不在本 PR 内，避免两个主题混在一起。
4. **评测语料来源**：`countbot.cn/docs/` 官方文档站全量 52 页（本项目自己的公开站点），无版权/隐私问题。
5. **口语语义类问题只缓解未消除**（需要向量检索）；"文档里没有就说没有"的拒答未实现——均列入未来计划，本 PR 不做承诺。

---

## 二、分支与提交计划（推荐方案 A）

### 2.1 为什么不能直接发当前分支

当前 `feature/rag-enhancement` 与 `main` 的 diff 有 **83 文件 / 23,872 行**，其中混着：

- `docs/rag/` 全套内部文档（primer/architecture/interview-narrative/open-risks/**audit-prompts.md** 等 AI 协作过程资产）——**绝不能进 PR**；
- `rag-bench/results/*-detail.json`（约 1.2 万行实验明细）——噪音；
- `rag-bench/corpus/`（52 页抓取副本）——可要可不要；
- 评测脚本与报告头部引用 `docs/rag/test-plan.md`——指向不存在的文件（docs 不进 PR）。

**维护者 review 的其实是这几百行生产代码**；把 2.3 万行内部资产一起推上去，等于邀请他别看了。

### 2.2 目标 PR 结构

```
PR: [feat] Wiki chunk-level retrieval（引用 issue #107）
├── Commit 1  feat(wiki): chunk-level BM25 retrieval + [slug#section] provenance
│             生产代码 4 个新文件 + wiki/tool.py（+100 行）+ tests/rag/ 2 文件
├── Commit 2  bench(wiki-rag): reproducible 60-question doc-vs-chunk evaluation
│             评测 6 个文件（自包含化后），详见 2.4
└── （约 700-1,600 行总 diff，全部与特性相关）
```

### 2.3 执行命令（在 countbot-rag 目录）

```bash
cd /Users/daniel/project/countbot-rag

# 0) 先确认 fork 与上游关系
git fetch origin upstream          # upstream = countbot-ai/CountBot
git fetch upstream main

# 1) 从 upstream/main 建干净分支（不继承任何内部历史）
git switch -c pr/wiki-chunk-retrieval-v1 upstream/main

# 2) 移植两个纯代码 commit（800b1f0 feat + 90e9327 gap-fix，按序）
git cherry-pick 800b1f0
git cherry-pick 90e9327

# 3) 精简提交信息为对上游友好的单一 feat commit
git rebase -i upstream/main        # squash 两个为 1 个，message 见 2.5

# 4) 评测资产（Commit 2）：从 feature/rag-enhancement 检出所需文件
git checkout feature/rag-enhancement -- \
  rag-bench/scripts/fetch_corpus.py \
  rag-bench/scripts/run_g0.py \
  rag-bench/scripts/run_g1.py \
  rag-bench/questions.jsonl \
  rag-bench/results/g0-baseline.md \
  rag-bench/results/g1-chunked.md

# 5) 自包含化清理（见 2.4），然后提交
git add -A && git commit -m "bench(wiki-rag): ..."   # message 见 2.5

# 6) 推送 fork 并开 PR
git push origin pr/wiki-chunk-retrieval-v1
# 打开 GitHub：compare pr/wiki-chunk-retrieval-v1 ... countbot-ai/CountBot:main
```

> 注意：`upstream/main` 若比本地 `main`（62486dd）新，先确认没有冲突再 cherry-pick。
> 若上游有新 commit 与 `wiki/tool.py`/`index.py` 冲突，在 cherry-pick 后解决冲突，并把改动点写进 PR 描述。

### 2.4 自包含化清理（评测 6 文件的必改项）

- `rag-bench/scripts/run_g0.py`：删 docstring 中的 `口径来源：docs/rag/test-plan.md（…）`，换为自包含说明：
  `口径：L1 程序化打分（recall@k/MRR/top1/注入 token 量），无 LLM 判分，任何人可复现。`
- `rag-bench/results/g0-baseline.md`：删 `> 口径来源：...` 行与 `分支：feature/rag-enhancement` 字样；`运行日期`保留。
- `rag-bench/results/g1-chunked.md`：删 `分支：feature/rag-enhancement` 字样。
- 脚本/报告中的 `G0/G1/M1` 代号可保留（PR 描述会说明"G0 = 改前的整篇检索、G1 = 改后的分块检索"）。

### 2.5 commit message 建议

```
feat(wiki): chunk-level retrieval with section provenance, behind COUNTBOT_RAG_CHUNKS

Splits each wiki doc by Markdown headings (long sections re-split by
paragraph with overlap) and indexes each chunk as a small doc on the
existing BM25Index - no algorithm rewrite, no new third-party deps.

Retrieval unit changes from whole doc to chunk:
- context injection 12,334 -> 910 est tokens avg (-92.6%)
- direct-question top1 0.067 -> 0.533; production hit rate 56% -> 82%
- every result carries [slug#section] provenance (was 0%)

Disabled by default (COUNTBOT_RAG_CHUNKS=1 to enable) so existing
behaviour is unchanged. AgentLoop / ToolRegistry / Cron untouched.

Tests: tests/rag/ 34 new + 1 existing = 35/35.
Docs/bench: see issue #107; reproducible eval in rag-bench/.

Refs: #107
```

### 2.6 不进 PR 的清单与理由

| 内容 | 理由 |
|---|---|
| `docs/rag/` 全部（含 audit-prompts.md） | 内部工作文档/AI 协作资产，与开源交付无关 |
| `rag-bench/results/*-detail.json` | 1.2 万行实验明细，噪音 |
| `rag-bench/corpus/` | 可再抓取（fetch_corpus.py），避免一次性增大 3,000+ 行 diff |
| `rag-bench/eval_config.yaml`、`l1_eval.py`、`requirements-eval.txt` | M2（向量/外部基准）工具，超前且引用内部路径 |
| `.gitignore` 修改 | `.codebuddy/` 为本地 IDE 数据，`requirements-eval.txt` 仅服务未纳入文件 |
| `jieba` 恢复（requirements.txt 第 63 行） | 独立一行小 PR（issue 已声明），避免两个主题混杂 |

---

## 三、PR 描述正文（可直接粘贴）

```markdown
Closes/relates: #107

## What & why

Wiki answers currently retrieve whole documents by BM25 and inject the
full text of top-3 docs into every prompt (avg 12,334 est tokens, max
45,944 - nearly a fifth of the whole corpus). Three concrete problems
are quantified in #107 (paraphrase-only queries miss, multi-doc
questions incomplete, no answer-when-absent).

This PR changes only the **retrieval unit**: docs are split by Markdown
headings into chunks (long sections re-split by paragraph, code fences
kept intact), and each chunk is indexed as a small document on the
existing `BM25Index`. No algorithm rewrite, no new dependencies.

## Result (same 60 questions, only the retrieval unit changed)

| metric | doc-level (before) | chunk-level (after) |
|---|---|---|
| context injected (avg) | 12,334 est tokens | 910 (−92.6%) |
| context injected (max) | 45,944 | 1,720 |
| direct-question top1 | 0.067 | 0.533 |
| paraphrase recall@5 | 0.20 | 0.60 |
| production hit rate (50 answerable) | 28/50 (56%) | 41/50 (82%) |
| provenance `[slug#section]` | none | 100% |

Known small regressions (1/60 questions) and why they are not real
answer regressions are listed in #107.

## Changes

- `backend/modules/rag/` (new): `chunker.py` (pure-stdlib heading
  splitter), `stores/bm25_store.py` (chunk-level adapter over the
  existing BM25Index), `service.py` (mtime incremental sync + single-doc
  rebuild hooks)
- `backend/modules/wiki/tool.py`: `COUNTBOT_RAG_CHUNKS` flag (default
  off = current behaviour), search/ask delegation, sync hooks
- `tests/rag/`: 34 new tests (chunker unit tests + switch regression:
  flag-off parity with doc-level output, poisoned-import survival,
  single-doc rebuild semantics)
- `rag-bench/`: reproducible 60-question evaluation harness + results

## Compatibility

- Feature flag default off: unset `COUNTBOT_RAG_CHUNKS` -> behaviour is
  identical to before, verified by regression tests
- `AgentLoop` / `ToolRegistry` / `Cron` untouched
- Chunk index is a derived artifact (`workspace/wiki/chunk_index.json`),
  deletable and rebuildable; wiki source files unaffected

## How to verify

```bash
pytest tests/ -q                    # 35/35

# optional: reproduce the evaluation (fetches public docs)
cd rag-bench
python3 scripts/fetch_corpus.py     # downloads countbot.cn/docs (52 pages)
python3 scripts/run_g0.py           # doc-level baseline (before)
python3 scripts/run_g1.py           # chunk-level (this PR)
```

## Not in this PR (see #107 for plans)

- un-commenting `jieba>=0.42` in requirements.txt (one-line fix,
  separate PR) - default installs currently fall back to per-char
  tokenization, making the numbers above an upper bound
- vector retrieval / refusal handling / memory semantic search - each
  independent, default-off, can be adopted or declined separately

## Known trade-offs

- `ChunkedBM25Index` serialization touches a few private fields of the
  upstream `BM25Index` (same pattern as its own save/load); if that
  class is refactored, the adapter needs to follow. Covered by tests.
- BM25's absolute score threshold (0.5) applies to chunk search too;
  measured hit rate 56% -> 82% shows no practical impact.
```

---

## 四、未来计划说明（写给维护者的"下一步"）

建议在 PR 描述中已内嵌简版（见上"Not in this PR"）。若想在 issue #107 追加评论，用这个（可省）：

> Thanks for the go-ahead. Plan of record, each step independent and
> default-off so it can be merged or declined separately:
> 1. this PR - chunked retrieval (ready now)
> 2. one-line fix to un-comment `jieba>=0.42` (default installs run a
>    degraded per-char tokenizer today)
> 3. vector + hybrid retrieval (only if measurements justify it; if not,
>    I'll post the numbers and stop at chunking rather than force it)
> 4. semantic memory search
> Happy to adapt scope to whatever fits the project.

---

## 五、发 PR 前检查清单

- [ ] 新分支基于 `upstream/main` 最新（`git fetch upstream && git merge-base --is-ancestor upstream/main HEAD`）
- [ ] diff 只含特性文件（`git diff --stat upstream/main...HEAD`），无 `docs/rag/`、无 detail json、无 `.gitignore`
- [ ] 评测文件已自包含化（无指向不存在文档的引用行）
- [ ] `pytest tests/ -q` 全绿（35/35）
- [ ] 生产代码以 `upstream/main` 为基准再 diff 一遍，确认无 merge 冲突
- [ ] PR 描述已粘贴（含"Not in this PR"与"Known trade-offs"——诚实部分最能建立信任）
- [ ] commit message 引用 `#107`
- [ ] 推送后 PR base 确认是 `countbot-ai/CountBot:main`（不是 `ddggkkcc`）
- [ ] 发完在 issue #107 评论里贴 PR 链接（无专人维护，评论是唯一的通知渠道）

---

## 六、发给维护者之前请自己再想一遍的三件事

1. **他可能不跑任何命令**：数字表 + 回归测试的存在本身要能说服他。PR 描述首屏（摘要+结果表）就是全部论据，别让他翻页。
2. **他可能只扫 commit 列表**：2 个 commit、message 干净、无"AI 痕迹"（无 M1/M0/代号堆砌）的 PR 观感完全不同。
3. **他可能搁置几周**：一切自包含（复现命令、预期行为、回滚方式都在 PR 描述里），别留"等回复才能验证"的悬空问题。
