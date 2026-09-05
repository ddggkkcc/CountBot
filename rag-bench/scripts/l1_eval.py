#!/usr/bin/env python3
"""L1 检索组件评测 —— ranx 驱动的程序化打分

对应用途：
  docs/rag/archive/test-plan.md §4.1   L1 检索组件测试（G0–G3 对照）
  docs/rag/archive/eval-datasets.md §4 外部旁证配置的 L1 部分（C-MTEB 中文基准）

设计原则：
  1. 程序化打分，不依赖 LLM judge —— 最可复现（archive/test-plan.md §2.2）
  2. 单变量对比：一次只改一处，before/after 跑同一批题
  3. 口径集中在 rag-bench/eval_config.yaml，实验卡片引用版本号保证可比

与 run_g0.py / run_g1.py 的分工：
  run_g*.py   —— 自建 60 题在自建语料上的 G 轮次实验（主论据）
  l1_eval.py  —— 公开中文基准（C-MTEB 等）上的横向对照（外部旁证，非门禁）

用法：
  # 对比多个 run
  python l1_eval.py --qrels ../data/qrels.txt --run G1=../data/g1.txt --run G2=../data/g2.txt

  # 从 CountBot 现役 BM25 索引直接生成 run 并评测
  # （语料 JSONL 每行含 id/title/content/tags，可由 C-MTEB corpus 转换得到）
  python l1_eval.py --qrels ../data/qrels.txt --bm25-corpus ../data/corpus.jsonl \
                    --queries ../data/queries.json --tag G1

  # 自检（合成数据，验证环境与脚本可用）
  python l1_eval.py --self-test

数据格式（TREC）：
  qrels: <qid> <iteration> <docid> <relevance>
  run  : <qid> Q0 <docid> <rank> <score> <tag>

注：本脚本默认对接文档级 BM25Index（wiki/index.py）。
块级 ChunkedBM25Index（G1+ 轮次）由 --chunk-run 驱动：
  python l1_eval.py --chunk-run G1 --write-qrels ../data/qrels-chunk.txt
qrels 由 questions.jsonl 的 source_pages 映射构造（Phase 1 任务 1），
每题 relevant = 其源文档 slug 的全部 chunk_id（文档级到块级的自然扩张）。
G2 向量 / G3 混合轮次在此之上新增对应 build_*_run 函数，
口径（top_k=50、阈值绕过、同一 qrels）保持与本文件一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

try:
    from ranx import Qrels, Run, compare, evaluate
except ImportError:
    sys.exit(
        "缺少 ranx。请安装评测依赖（不并入主 requirements.txt）：\n"
        "  pip install -r rag-bench/requirements-eval.txt"
    )

DEFAULT_METRICS = ["ndcg@10", "recall@50", "mrr@10", "map@10"]
# 块级轮次（G1+）口径：对齐生产 ask 注入的 top-6（Phase 1 任务 1）
CHUNK_METRICS = ["hit_rate@6", "mrr@6", "recall@6"]
SCRIPT_DIR = Path(__file__).resolve().parent
BENCH = SCRIPT_DIR.parent          # rag-bench/
REPO_ROOT = BENCH.parent           # countbot-rag 仓库根


# ────────────────────────────────────────────
# 数据加载
# ────────────────────────────────────────────

def load_qrels(path: str | Path) -> Qrels:
    return Qrels.from_file(str(path), kind="trec")


def load_run(path: str | Path, name: str | None = None) -> Run:
    run = Run.from_file(str(path), kind="trec")
    if name:
        run.name = name
    return run


def parse_run_specs(specs: list[str]) -> dict[str, Run]:
    """解析 TAG=path 形式的 run 参数，返回 {tag: Run}"""
    runs: dict[str, Run] = {}
    for spec in specs:
        if "=" not in spec:
            sys.exit(f"run 参数格式应为 TAG=path，收到：{spec}")
        tag, path = spec.split("=", 1)
        runs[tag] = load_run(path.strip(), name=tag.strip())
    return runs


# ────────────────────────────────────────────
# 与 CountBot 现役代码对接
# ────────────────────────────────────────────

def load_bm25_index_class():
    """动态加载 backend/modules/wiki/index.py 的 BM25Index

    用 importlib 而非 from backend...import，避免触发 backend 包的其他依赖，
    也避免评测脚本被后端包结构变动牵连。
    """
    index_py = REPO_ROOT / "backend" / "modules" / "wiki" / "index.py"
    if not index_py.exists():
        sys.exit(f"未找到 BM25 实现：{index_py}")
    spec = importlib.util.spec_from_file_location("cb_bm25", index_py)
    if spec is None or spec.loader is None:
        sys.exit(f"无法加载模块：{index_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BM25Index


def check_jieba() -> None:
    """jieba 是可选依赖，缺失会让基线测到'单字分词'而非线上真实行为

    对应 archive/primer.md §2.7：主 requirements.txt 中 jieba 被注释，
    --no-jieba 对照组已实测 recall@5 0.80 → 0.53 的差距。
    """
    try:
        import jieba  # noqa: F401
    except ImportError:
        print(
            "⚠️  未安装 jieba。BM25Index 将回退为单字分词，"
            "测出来的基线不代表线上真实行为（见 archive/primer.md §2.7）。\n"
            "   修复：pip install -r rag-bench/requirements-eval.txt",
            file=sys.stderr,
        )


def build_bm25_run(
    corpus_path: str | Path,
    queries: dict[str, str],
    top_k: int = 50,
    bypass_threshold: bool = True,
    name: str = "bm25",
) -> Run:
    """用 CountBot 现役 BM25Index 生成 TREC run

    ⚠️ 关键：BM25Index.search() 内置两重结果过滤——
        相对阈值 score >= max_score * min_score_ratio(默认 0.3)
        绝对阈值 score >= SCORE_THRESHOLD(默认 0.5)
       这会让 top_k=50 常常只返回个位数结果。
       若不绕过，recall@50 会被严重低估，测到的是"阈值行为"而非"检索能力"，
       基线数据将不可信。

    bypass_threshold=True 时把两重阈值置 0，取完整排序。

    已知限制：绕过阈值后，候选集仍只包含"至少命中一个查询 token"的文档。
    零词重叠文档 BM25 无法召回——这是词法检索的固有上限，
    也正是 G2 向量需要证明的增量空间（语义鸿沟，archive/primer.md §2.3）。
    """
    BM25Index = load_bm25_index_class()
    check_jieba()

    index = BM25Index()
    if bypass_threshold:
        # 实例属性覆盖类常量，仅影响本次评测
        index.SCORE_THRESHOLD = 0.0

    with open(corpus_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            doc = json.loads(line)
            index.add_document(
                doc_id=str(doc["id"]),
                title=doc.get("title", ""),
                content=doc.get("content", ""),
                tags=doc.get("tags", []),
            )

    run = Run()
    run.name = name
    min_ratio = 0.0 if bypass_threshold else 0.3
    for qid, query in queries.items():
        results = index.search(query, top_k=top_k, min_score_ratio=min_ratio)
        for doc_id, score in results:
            run.add_score(str(qid), str(doc_id), float(score))

    return run


# ────────────────────────────────────────────
# 块级（G1+ 轮次）：ChunkedBM25Index + 自建 60 题
# ────────────────────────────────────────────

def build_chunk_index(corpus_dir: str | Path):
    """从 rag-bench/corpus 建 ChunkedBM25Index

    与 run_g1.py 完全同口径：manifest 的原始 slug（如 core/memory）直接作为
    文档标识——questions.jsonl 的 source_pages 去 .md 后与之精确相等，
    这是块级 qrels 映射成立的前提（已核实，见 implementation-plan §2）。
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from backend.modules.rag.stores import ChunkedBM25Index  # noqa: E402

    corpus = Path(corpus_dir)
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    store = ChunkedBM25Index()
    for e in manifest:
        content = (corpus / f"{e['slug']}.md").read_text(encoding="utf-8")
        store.add_document(e["slug"], e["title"], content, [e.get("section", "")])
    return store


def load_questions_jsonl(path: str | Path) -> list[dict]:
    """读自建 60 题（rag-bench/questions.jsonl）"""
    return [
        json.loads(l)
        for l in Path(path).read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def build_qrels_from_questions(questions: list[dict], store) -> Qrels:
    """块级 qrels：每题 relevant = source_pages 各 slug 的全部 chunk_id

    映射规则：source_pages（core/memory.md）去 .md == manifest slug ==
    ChunkedBM25Index 的文档标识 → 该文档的每个 chunk 都算相关（文档级
    标注的自然块级扩张，不做节级人工标注）。

    negative 题无 source_pages，不进入 qrels（拒答行为的评测归
    run_crag_eval.py，不与检索指标混流）。
    """
    qrels = Qrels()
    missing: list[tuple[str, str]] = []
    for q in questions:
        if q.get("type") == "negative":
            continue
        for page in q.get("source_pages", []):
            slug = page[:-3] if page.endswith(".md") else page
            chunk_ids = store._slug_registry.get(slug, {}).get("chunk_ids", [])
            if not chunk_ids:
                missing.append((q["id"], slug))
                continue
            for cid in chunk_ids:
                qrels.add_score(q["id"], cid, 1)
    if missing:
        print(
            f"⚠️  {len(missing)} 个 (题目, slug) 在索引中无对应 chunk：{missing[:5]}",
            file=sys.stderr,
        )
    return qrels


def write_qrels_trec(qrels: Qrels, path: str | Path) -> None:
    """qrels 落盘为 TREC 格式（<qid> 0 <chunk_id> 1），供复现与其他 run 对照"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for qid in sorted(qrels.qrels):
            for cid in sorted(qrels.qrels[qid]):
                f.write(f"{qid} 0 {cid} 1\n")


def build_chunk_run(
    store,
    queries: dict[str, str],
    top_k: int = 50,
    bypass_threshold: bool = True,
    name: str = "G1-chunk",
) -> Run:
    """用 ChunkedBM25Index 生成块级 TREC run

    阈值绕过口径与 build_bm25_run 一致（SCORE_THRESHOLD / min_score_ratio
    双双置 0），否则 recall 类指标测到的是"阈值行为"而非"检索能力"。
    top_k 默认 50：对齐 HybridRetriever 的候选池深度（BM25 top-50 → 精排 top-6），
    6 以内的指标（hit_rate@6 / mrr@6 / recall@6）不受深度影响。
    """
    check_jieba()
    if bypass_threshold:
        store._bm25.SCORE_THRESHOLD = 0.0

    run = Run()
    run.name = name
    min_ratio = 0.0 if bypass_threshold else 0.3
    for qid, query in queries.items():
        for chunk_id, score in store.search(query, top_k=top_k, min_score_ratio=min_ratio):
            run.add_score(str(qid), str(chunk_id), float(score))

    return run


# ────────────────────────────────────────────
# 评测与对比
# ────────────────────────────────────────────

def summarize(qrels: Qrels, runs: dict[str, Run], metrics: list[str]) -> dict:
    """各 run 的指标汇总，返回 {tag: {metric: value}}"""
    if len(runs) == 1:
        tag, run = next(iter(runs.items()))
        return {tag: evaluate(qrels, run, metrics)}
    # ranx 的 compare() 只接受 List[Run]，run 名取自 Run.name
    report = compare(qrels=qrels, runs=list(runs.values()), metrics=metrics, max_p=0.01)
    raw = report.to_dict()
    return {name: raw[name]["scores"] for name in raw["model_names"]}


def per_query_diff(
    qrels: Qrels,
    baseline: Run,
    target: Run,
    metrics: list[str],
    main_metric: str = "ndcg@10",
) -> list[dict]:
    """逐 query 对比，识别变好 / 变坏的题目（实验卡片"变好变坏"栏）

    注意：对 24K 量级查询会较慢，大规模数据集建议先抽样。
    """
    diffs: list[dict] = []
    query_ids = sorted(set(qrels.qrels.keys()) & set(target.run.keys()))

    for qid in query_ids:
        sub_qrels = Qrels()
        if qid in qrels.qrels:
            for doc_id, score in qrels.qrels[qid].items():
                sub_qrels.add_score(qid, doc_id, score)

        sub_base = Run()
        if qid in baseline.run:
            for doc_id, score in baseline.run[qid].items():
                sub_base.add_score(qid, doc_id, score)

        sub_target = Run()
        if qid in target.run:
            for doc_id, score in target.run[qid].items():
                sub_target.add_score(qid, doc_id, score)

        before = evaluate(sub_qrels, sub_base, metrics).get(main_metric, 0.0)
        after = evaluate(sub_qrels, sub_target, metrics).get(main_metric, 0.0)
        diffs.append(
            {
                "query_id": qid,
                "before": round(before, 4),
                "after": round(after, 4),
                "delta": round(after - before, 4),
            }
        )

    diffs.sort(key=lambda r: r["delta"])
    return diffs


# ────────────────────────────────────────────
# 输出
# ────────────────────────────────────────────

def write_report(out_dir: Path, summary: dict, diff: list[dict] | None, metrics: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = ["# L1 检索评测报告", "", f"- 指标口径：{', '.join(metrics)}", ""]
    header = "| run | " + " | ".join(metrics) + " |"
    lines += [header, "|" + "---|" * (len(metrics) + 1)]
    for tag, scores in summary.items():
        cells = []
        for m in metrics:
            val = scores.get(m)
            cells.append(f"{val:.4f}" if isinstance(val, (int, float)) else "-")
        lines.append(f"| {tag} | " + " | ".join(cells) + " |")
    lines.append("")

    if diff:
        better = [d for d in diff if d["delta"] > 0.001]
        worse = [d for d in diff if d["delta"] < -0.001]
        lines += [
            "## 变好 / 变坏",
            "",
            f"- 变好：{len(better)} 题",
            f"- 变坏：{len(worse)} 题（须逐条说明原因或记为已知限制）",
            "",
        ]
        if worse:
            lines += ["### 变坏明细（最严重 20 条）", "", "| query | before | after | delta |", "|---|---|---|---|"]
            for d in worse[:20]:
                lines.append(f"| {d['query_id']} | {d['before']} | {d['after']} | {d['delta']} |")
            lines.append("")

    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    if diff:
        import csv

        with open(out_dir / "diff.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["query_id", "before", "after", "delta"])
            writer.writeheader()
            writer.writerows(diff)

    print("\n".join(lines))
    print(f"\n报告已写入：{out_dir}")


# ────────────────────────────────────────────
# 自检
# ────────────────────────────────────────────

def self_test() -> int:
    """用合成数据跑通全流程，验证环境与脚本可用（不需要下载数据集）"""
    import tempfile

    print("运行自检：构造 20 个查询 × 200 篇文档的合成数据...\n")

    qrels = Qrels()
    base_run = Run()
    base_run.name = "G0"
    good_run = Run()
    good_run.name = "G1"

    for q in range(20):
        qid = f"q{q}"
        gold = f"d{q}"
        qrels.add_score(qid, gold, 1)

        # 基线：黄金文档排在第 8 位（次优基线）
        for rank, doc_id in enumerate([f"d{(q + i) % 200}" for i in range(50)]):
            if doc_id != gold:
                base_run.add_score(qid, doc_id, 50.0 - rank)
        base_run.add_score(qid, gold, 50.0 - 7)

        # 改进后：黄金文档排在第 1 位
        good_run.add_score(qid, gold, 100.0)
        for rank, doc_id in enumerate([f"d{(q + i) % 200}" for i in range(49)]):
            if doc_id != gold:
                good_run.add_score(qid, doc_id, 50.0 - rank)

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "report"
        summary = summarize(qrels, {"G0": base_run, "G1": good_run}, DEFAULT_METRICS)
        diff = per_query_diff(qrels, base_run, good_run, DEFAULT_METRICS)
        write_report(out_dir, summary, diff, DEFAULT_METRICS)

    better = sum(1 for d in diff if d["delta"] > 0.001)
    worse = sum(1 for d in diff if d["delta"] < -0.001)
    ok = better == 20 and worse == 0
    print(f"\n自检结论：{'通过' if ok else '失败'}（变好 {better} / 变坏 {worse}，期望 20 / 0）")
    if not ok:
        print("  ↑ 若为 0/0，通常是 ranx 版本 API 差异，请核对指标名与 ranx 版本")
    return 0 if ok else 1


# ────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="L1 检索组件评测（ranx）")
    parser.add_argument("--qrels", help="qrels 文件路径（TREC 格式）；--chunk-run 模式可省略，自动构造")
    parser.add_argument("--run", action="append", default=[], metavar="TAG=PATH",
                        help="run 文件，可重复：--run G1=data/g1.txt --run G2=data/g2.txt")
    parser.add_argument("--bm25-corpus", metavar="JSONL",
                        help="语料 JSONL（每行含 id/title/content/tags），用现役 BM25 生成 run")
    parser.add_argument("--queries", metavar="JSON", help='查询文件 {qid: query}，配合 --bm25-corpus')
    parser.add_argument("--tag", default="G1", help="--bm25-corpus 生成的 run 名称")
    parser.add_argument("--chunk-run", metavar="TAG",
                        help="块级模式：用 ChunkedBM25Index（--corpus）对自建 60 题生成 run 并评测，"
                             "qrels 由 questions.jsonl 自动构造")
    parser.add_argument("--corpus", default=str(BENCH / "corpus"),
                        help="块级模式语料目录（含 manifest.json），默认 rag-bench/corpus")
    parser.add_argument("--questions-jsonl", default=str(BENCH / "questions.jsonl"),
                        help="自建题目文件，默认 rag-bench/questions.jsonl")
    parser.add_argument("--write-qrels", metavar="PATH",
                        help="把块级 qrels 写成 TREC 文件（供复现与其他 G 轮次对照）")
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--keep-threshold", action="store_true",
                        help="保留 BM25 内置阈值（默认绕过，详见 build_bm25_run 文档字符串）")
    parser.add_argument("--metrics", default=None,
                        help="逗号分隔指标；默认按模式取（文档级 ndcg@10,recall@50,mrr@10,map@10；"
                             f"块级 {','.join(CHUNK_METRICS)}）")
    parser.add_argument("--baseline", help="对照基线 tag，用于逐 query 变好变坏分析")
    parser.add_argument("--out", default=str(BENCH / "results" / "l1"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    runs = parse_run_specs(args.run)

    qrels = None
    if args.chunk_run:
        store = build_chunk_index(args.corpus)
        questions = load_questions_jsonl(args.questions_jsonl)
        # 检索指标只评正样本：negative 题无 relevant chunk（拒答归 run_crag_eval.py）
        queries = {q["id"]: q["question"] for q in questions if q["type"] != "negative"}
        qrels = build_qrels_from_questions(questions, store)
        if args.write_qrels:
            write_qrels_trec(qrels, args.write_qrels)
        runs[args.chunk_run] = build_chunk_run(
            store,
            queries,
            top_k=args.top_k,
            bypass_threshold=not args.keep_threshold,
            name=args.chunk_run,
        )
    elif args.qrels:
        qrels = load_qrels(args.qrels)

    if qrels is None:
        parser.error("需要 --qrels、--chunk-run 或 --self-test 之一")

    if args.bm25_corpus:
        if not args.queries:
            parser.error("--bm25-corpus 需要配合 --queries")
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
        runs[args.tag] = build_bm25_run(
            args.bm25_corpus,
            queries,
            top_k=args.top_k,
            bypass_threshold=not args.keep_threshold,
            name=args.tag,
        )

    if not runs:
        parser.error("没有可评测的 run，用 --run / --bm25-corpus / --chunk-run 提供")

    metrics = (
        [m.strip() for m in args.metrics.split(",") if m.strip()]
        if args.metrics
        else (CHUNK_METRICS if args.chunk_run else DEFAULT_METRICS)
    )
    summary = summarize(qrels, runs, metrics)

    diff = None
    if args.baseline and args.baseline in runs and len(runs) > 1:
        others = [t for t in runs if t != args.baseline]
        diff = per_query_diff(qrels, runs[args.baseline], runs[others[0]], metrics)

    write_report(Path(args.out), summary, diff, metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
