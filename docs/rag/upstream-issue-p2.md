# Upstream Issue #2: Hybrid Retrieval + Reranking（提案/讨论稿）

> 用途：粘贴到 countbot-ai/countbot 的 GitHub issue。发 issue 后在 #107 追加评论链接。
> 配套：`upstream-pr-p2.md`（PR 描述正文）。

---

**Title:** [RAG] Proposal: Hybrid Retrieval + Reranking to handle semantic and fuzzy queries

Closes/relates: #107 (chunk-level retrieval, merged) · this proposal is the follow-up.

## Background & Pain Point

The chunk-level BM25 retrieval shipped in #107 solved context explosion (−92.6% injected tokens) but keeps a structural blind spot: **it cannot cross the semantic gap**. When the user's phrasing differs from the document's terminology, BM25 retrieves nothing useful:

- On our 60-question terminology-based eval set (questions written with the docs' own vocabulary), BM25 scores Hit@6 = **0.82** — looks fine.
- On a new 30-question **colloquial set** (questions written the way real users actually ask — "怎么让外面的人连进来" instead of "配置远程访问认证", deliberately avoiding doc terminology), BM25 collapses to Hit@6 = **0.33**, with **57% of questions refused outright** ("not in the knowledge base") even though every answer exists in the corpus.

The terminology set is a developer's view of the system; the colloquial set is the user's. A retrieval upgrade that looks marginal on the first is transformative on the second — which is why we built both.

## Proposed Solution (4 layers, each independently toggleable)

```
query → BM25 top-50 ─┐
                      ├→ RRF fusion (k=60) → top-50 candidates
       Dense top-50 ──┘   (BGE-M3 via OpenAI-compatible API)
                              ↓
                     Reranker (bge-reranker-v2-m3, cross-encoder)
                              ↓ top-6
                     LLM Grader (all|none — refusal only)
                              ↓
                     Generate with [slug#section] citations
```

1. **Dense channel** (`embeddings.py` + `vector_store.py`): BGE-M3 via OpenAI-compatible `/v1/embeddings`, numpy brute-force cosine (1,423 chunks × 1024d ≈ 5.8 MB — millisecond-level, no vector DB needed at this scale).
2. **RRF fusion** (`retriever.py`): rank-based fusion, immune to score-scale mismatch between BM25 and cosine; serves as the **candidate pool** (top-50), not the final ranking.
3. **Precision ranking** (`reranker.py`): cross-encoder over the top-50 candidates → top-6. This is where the ordering quality comes from — RRF alone was net-negative on the injection window in our measurements.
4. **Grader shrink** (`tool.py`): the existing CRAG grader keeps only its refusal duty (`all|none`). It no longer filters chunks (that was an LLM doing a reranker's job at 200–400 tokens per call).

## Results (both eval sets, same system, only retrieval changed)

| Metric | Terminology set (60 q) | Colloquial set (30 q) |
|---|---|---|
| Hit@6 BM25 → hybrid+rerank | 0.82 → **0.90** | 0.33 → **0.83** |
| MRR@6 | 0.647 → **0.700** | 0.187 → **0.519** |
| End-to-end refusal rate (positives) | 20% → 16% | **57% → 3%** |
| Negative-sample refusal (anti-hallucination) | 10/10 → **10/10** (no regression) | — |
| Pairwise answer quality vs BM25 | win 23 / loss 9 / tie 26 | **win 21 / loss 7 / tie 2** |

A finding worth flagging for other contributors: **embedding choice interacts with query distribution**. BGE-M3 wins on the terminology set, but qwen3-embedding-8b scored higher on the colloquial set (hybrid Hit@6 0.80 vs 0.70). There is no universally best embedding; select against your query distribution, and we ship BGE-M3 as the default only because it tops C-MTEB.

## Backward Compatibility

Everything is behind env flags, **default off = byte-identical current behavior** (verified by regression tests):

- `COUNTBOT_RAG_CHUNKS` — chunk-level BM25 (shipped in #107)
- `COUNTBOT_RAG_HYBRID` — dense channel + RRF (this PR)
- `COUNTBOT_RAG_RERANK` — cross-encoder precision stage (this PR)

No new hard dependencies (numpy optional with pure-Python fallback; httpx already required). AgentLoop / ToolRegistry / Cron untouched.

## Open Question for Maintainers

During acceptance we caught a trade-off that we want the community's read on. The original grader prompt biased toward answering ("when unsure, prefer generating"), which halved misrefusals — but 3 questions flipped from *honest refusal* to *confidently wrong answers* (the corpus says "22 providers"; the system answered "two provider implementations" with full confidence).

We changed the bias to **"refuse when evidence is insufficient"** (wrong answers 4 → 2, misrefusals 5 → 8). Our reasoning: refusal is a retryable failure mode (the user rephrases); hallucination is not (the user believes it). The full before/after data is in the PR. Do you agree with this direction, or would you weight availability over answer-safety here?

Reproduction commands are in the PR description; the eval harness lives in `rag-bench/`.
