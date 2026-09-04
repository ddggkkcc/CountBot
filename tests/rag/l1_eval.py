#!/usr/bin/env python3
"""L1 检索组件评测 —— ranx 驱动的程序化打分

对应用途：
  docs/rag/test-plan.md §4.1   L1 检索组件测试（G0–G3 对照）
  docs/rag/eval-datasets.md §4 推荐量化组合拳的 L1 部分

设计原则：
  1. 程序化打分，不依赖 LLM judge —— 最可复现（test-plan.md §2.2）
  2. 单变量对比：一次只改一处，before/after 跑同一批题
  3. 口径集中在 eval_config.yaml，实验卡片引用版本号保证可比

用法：
  # 对比多个 run
  python l1_eval.py --qrels data/qrels.txt --run G0=runs/g0.txt --run G1=runs/g1.txt

  # 从 CountBot 现役 BM25 索引直接生成 run 并评测
  python l1_eval.py --qrels data/qrels.txt --bm25-corpus data/corpus.jsonl --tag G0

  # 自检（合成数据，验证环境与脚本可用）
  python l1_eval.py --self-test

数据格式（TREC）：
  qrels: <qid> <iteration> <docid> <relevance>
  run  : <qid> Q0 <docid> <rank> <score> <tag>
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
        "缺少 ranx。请安装评测依赖：\n"
        "  pip install -r tests/rag/requirements-rag.txt"
    )

DEFAULT_METRICS = ["ndcg@10", "recall@50", "mrr@10", "map@10"]
SCRIPT_DIR = Path(__file__).parent


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
    index_py = SCRIPT_DIR.parents[1] / "backend" / "modules" / "wiki" / "index.py"
    if not index_py.exists():
        sys.exit(f"未找到 BM25 实现：{index_py}")
    spec = importlib.util.spec_from_file_location("cb_bm25", index_py)
    if spec is None or spec.loader is None:
        sys.exit(f"无法加载模块：{index_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BM25Index


def check_jieba() -> None:
    """jieba 是可选依赖，缺失会让基线测到'单字分词'而非线上真实行为"""
    try:
        import jieba  # noqa: F401
    except ImportError:
        print(
            "⚠️  未安装 jieba。BM25Index 将回退为单字分词，"
            "测出来的 G0 基线不代表线上真实行为。\n"
            "   修复：pip install jieba",
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
       G0 基线数据将不可信。

    bypass_threshold=True 时把两重阈值置 0，取完整排序。

    已知限制：绕过阈值后，候选集仍只包含"至少命中一个查询 token"的文档。
    零词重叠文档 BM25 无法召回——这是词法检索的固有上限，
    也正是 G2 向量需要证明的增量空间。
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
# 评测与对比
# ────────────────────────────────────────────

def summarize(qrels: Qrels, runs: dict[str, Run], metrics: list[str]) -> dict:
    """各 run 的指标汇总"""
    if len(runs) == 1:
        tag, run = next(iter(runs.items()))
        return {tag: evaluate(qrels, run, metrics)}
    report = compare(qrels=qrels, runs=runs, metrics=metrics, max_p=0.01)
    return report.to_dict(orient="index")


def per_query_diff(
    qrels: Qrels,
    baseline: Run,
    target: Run,
    metrics: list[str],
    main_metric: str = "ndcg@10",
) -> list[dict]:
    """逐 query 对比，识别变好 / 变坏的题目（实验卡片"变好变坏"栏）

    注意：对 24K 量级查询会较慢，大规模数据集建议先抽样（--sample）。
    """
    diffs: list[dict] = []
    query_ids = sorted(set(qrels.qrels.keys()) & set(target.run.keys()))

    for qid in query_ids:
        sub_qrels = Qrels()
        for doc_id, score in qrels.qrels.get(qid, {}).items():
            sub_qrels.add_score(qid, doc_id, score)

        sub_base = Run()
        for doc_id, score in baseline.run.get(qid, {}).items():
            sub_base.add_score(qid, doc_id, score)

        sub_target = Run()
        for doc_id, score in target.run.get(qid, {}).items():
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

        # G0：黄金文档排在第 8 位（次优基线）
        for rank, doc_id in enumerate([f"d{(q + i) % 200}" for i in range(50)]):
            if doc_id != gold:
                base_run.add_score(qid, doc_id, 50.0 - rank)
        base_run.add_score(qid, gold, 50.0 - 7)

        # G1：黄金文档排在第 1 位（改进后）
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
    parser.add_argument("--qrels", help="qrels 文件路径（TREC 格式）")
    parser.add_argument("--run", action="append", default=[], metavar="TAG=PATH",
                        help="run 文件，可重复：--run G0=runs/g0.txt --run G1=runs/g1.txt")
    parser.add_argument("--bm25-corpus", metavar="JSONL",
                        help="语料 JSONL（每行含 id/title/content/tags），用现役 BM25 生成 run")
    parser.add_argument("--queries", metavar="JSON", help='查询文件 {qid: query}，配合 --bm25-corpus')
    parser.add_argument("--tag", default="G0", help="--bm25-corpus 生成的 run 名称")
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--keep-threshold", action="store_true",
                        help="保留 BM25 内置阈值（默认绕过，详见 build_bm25_run 文档字符串）")
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    parser.add_argument("--baseline", help="对照基线 tag，用于逐 query 变好变坏分析")
    parser.add_argument("--out", default=str(SCRIPT_DIR / "report"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.qrels:
        parser.error("需要 --qrels，或用 --self-test 自检")

    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    qrels = load_qrels(args.qrels)
    runs = parse_run_specs(args.run)

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
        parser.error("没有可评测的 run，用 --run 或 --bm25-corpus 提供")

    summary = summarize(qrels, runs, metrics)

    diff = None
    if args.baseline and args.baseline in runs and len(runs) > 1:
        others = [t for t in runs if t != args.baseline]
        diff = per_query_diff(qrels, runs[args.baseline], runs[others[0]], metrics)

    write_report(Path(args.out), summary, diff, metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
