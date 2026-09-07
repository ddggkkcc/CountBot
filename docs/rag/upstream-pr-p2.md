# Upstream PR #2: 混合检索 + 重排（PR 描述正文）

> 用途：粘贴到 GitHub PR 描述。分支基于 upstream/main 干净重建（只 cherry-pick 生产代码与评测资产，docs/rag/ 内部文档不进 PR——沿用 v1 手册纪律）。
> 配套：`upstream-issue-p2.md`（先发 issue，PR 引用之）。

---

**Title:** [Feature] RAG retrieval upgrade with dense retrieval, reranking, and bias-fixed grader

Closes: #<issue-number> (relates #107)

## What

Four new modules under `backend/modules/rag/`, plus a rework of the chunk-level ask pipeline in `wiki/tool.py`. All behind `COUNTBOT_RAG_HYBRID` / `COUNTBOT_RAG_RERANK` (default **off** — behavior identical to current main):

- **`embeddings.py`** — OpenAI-compatible `/v1/embeddings` client (default model `BAAI/bge-m3`). Batches ≤16, validates dimension against config (a wrong model/dim pair fails loudly instead of corrupting the index). Returns `None` when unconfigured → callers fall back to pure BM25.
- **`stores/vector_store.py`** — cosine top-k over a chunk-id keyed vector pool. numpy-first with a pure-Python fallback (`try: import numpy`), so no new hard dependency. Persists as `.npz` (numpy) or `.json`, auto-detected on load. At the current corpus scale (1,423 chunks × 1024d ≈ 5.8 MB) brute-force search is millisecond-level — an ANN index or vector DB would be pure operational overhead, so none is used. The interface (`search(query_vec, top_k)`) is designed to be swappable if the corpus ever grows 100×.
- **`retriever.py`** — `HybridRetriever`: BM25 top-50 ⊕ Dense top-50 → RRF fusion (k=60) → top-6. RRF works on ranks only, so BM25 scores and cosine similarities never need calibration against each other. The dense channel degrades to BM25-only on any failure.
- **`reranker.py`** — cross-encoder precision stage (`BAAI/bge-reranker-v2-m3` via OpenAI-compatible `/rerank`): top-50 candidates → top-6. Attaches `rerank_score` while preserving the retrieval-layer score (different scales must not mix). On failure, falls back to retrieval order **and still routes through the grader** — a degraded path never bypasses the refusal check.
- **`wiki/tool.py`** — the ask pipeline becomes: retrieval top-50 → rerank top-6 → grader → generate. The grader (`_grade_chunks`) is shrunk to a pure **all|none refusal decision** — it no longer filters chunks (that was an LLM doing a reranker's job at 200–400 tokens per call). Grader/rerank results are LRU-cached per `(query, chunk_ids)`.

Also included: `rag-bench/` evaluation harness updates (a 30-question colloquial eval set, G2/G3/G4 run builders, an end-to-end judge harness with refusal attribution and pairwise comparison).

## Why

The #107 chunk-level BM25 fixed context explosion but kept BM25's structural blind spot: **lexical retrieval cannot bridge phrasing differences**. Our first eval set couldn't show this — its questions were written *using the docs' own vocabulary* (a developer's view). We built a second set of 30 questions written the way users actually ask ("怎么让外面的人连进来" not "配置远程访问认证"), deliberately free of doc terminology.

Same system, same corpus, only the query distribution changed:

| | Terminology set | Colloquial set |
|---|---|---|
| BM25 Hit@6 | 0.82 | **0.33** |
| BM25 end-to-end refusal (positives) | 20% | **57%** |

57% of colloquial questions were refused outright despite every answer existing in the corpus. Hybrid+rerank lifts that set to Hit@6 0.83 / refusal 3%. **The value of semantic retrieval is a function of the query distribution — a terminology-only eval set underestimates it by an order of magnitude.** We ship both sets in `rag-bench/` so future retrieval changes are measured against both.

## How to Test

```bash
pytest tests/ -q    # 211 passed

# Optional: reproduce the retrieval evaluation (needs SiliconFlow or any
# OpenAI-compatible endpoint serving bge-m3 / bge-reranker-v2-m3)
cd rag-bench
python3 scripts/fetch_corpus.py          # 52 public docs from countbot.cn

export COUNTBOT_RAG_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
export COUNTBOT_RAG_EMBEDDING_API_KEY=<your-key>
export COUNTBOT_RAG_RERANK_BASE_URL=$COUNTBOT_RAG_EMBEDDING_BASE_URL
export COUNTBOT_RAG_RERANK_API_KEY=<your-key>

# terminology set: G1 vs G4 (rerank gates)
python3 scripts/l1_eval.py --chunk-run G1 --rerank-run G4 \
    --write-runs data/runs-p2 --out results/l1-p2-rerank

# colloquial set: three-tier progression
python3 scripts/l1_eval.py --chunk-run G1 --hybrid-run G3 --rerank-run G4 \
    --questions-jsonl questions-fuzzy.jsonl \
    --write-runs data/runs-fuzzy-bge --out results/l1-fuzzy-bge
```

Runtime behavior without the env vars is identical to main (regression tests assert this, including a poisoned-import survival check).

## Performance & Trade-offs

**Retrieval quality** (Hit@6 / MRR@6, programmatic scoring, no LLM):

| Set | G1 BM25 | Final (hybrid+rerank) |
|---|---|---|
| Terminology 60q | 0.8200 / 0.6473 | **0.9000 / 0.7000** |
| Colloquial 30q | 0.3333 / 0.1867 | **0.8333 / 0.5194** |

**End-to-end** (LLM judge, 5-way verdict, pairwise vs the BM25 pipeline):

| | Terminology set | Colloquial set |
|---|---|---|
| Negative-sample refusal (anti-hallucination) | **10/10 preserved** | — |
| Positive refusal rate | 20% → 16% | **57% → 3%** |
| Correct+partial | 70% → 78% | 33% → 83% |
| Answers with `[slug#section]` citations | 72% → 82% | 33% → 80% |
| Pairwise vs BM25 | win 23 / loss 9 / tie 26 | **win 21 / loss 7 / tie 2** |

**Honest regressions we recorded rather than hid:**

1. **The refusal-vs-hallucination trade-off, made explicit.** Adding the dense channel fed the grader more "topically plausible" chunks; the original grader bias ("when unsure, prefer generating") produced 3 questions that flipped from honest refusal to confidently wrong answers. We fixed this with a two-part change — grader input truncation raised from 120 to 300 chars (a fact at offset 209 was literally invisible to the grader), and the bias flipped to *"when a specific fact/number/command is asked and the results cannot supply it, judge none."* Result: wrong+hallucinated **4 → 2**, misrefusals 5 → 8. We consider this the right side of the trade: **refusal is a retryable failure mode (the user rephrases); hallucination is not (the user believes it).** The full data is in `rag-bench/results/`.
2. **Embedding choice interacts with query distribution.** BGE-M3 (our default, C-MTEB leader) beats qwen3-embedding-8b on the terminology set but trails it on the colloquial set (hybrid Hit@6 0.70 vs 0.80). There is no universally best embedding; pick against your query distribution.
3. **Known limitation — top-6 boundary cuts.** One question's fact chunk ranked 7th among rerank candidates and was cut; the grader then saw a plausible-but-wrong number. This is a precision-ranking problem (not a grader problem — we verified the fact never entered the grader's view) and is left as future work (dynamic top-k / fact-chunk boosting).
4. **LLM calls per question** is ~2.2 (grader + generation). We prototyped a confidence gate (skip the grader when rerank top-1 ≥ threshold) and **disabled it after calibration**: a negative-sample question ("deploy to Kubernetes") scored 0.99 against the Docker-deployment chunk — *higher* than the positive question on the same chunk (0.90). Cross-encoders measure topical match, not answerability; no score threshold separates them. The mechanism remains in the code behind a config constant, disabled by default, with the calibration data in the commit message.

## Scope

Not in this PR (each deliberate, with measurements to back them): vector databases (scale mismatch — 1.4K chunks), GraphRAG (no entity network in a 30-doc technical corpus), LangGraph (CountBot is itself an agent framework), Contextual Retrieval (heading paths already provide the context; written re-trigger conditions in the issue).
