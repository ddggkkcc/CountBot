#!/usr/bin/env python3
"""
CountBot 幻觉测试运行器

用法:
  # 1. 规则模式（用 mock 响应演示评估器）
  python tests/hallucination/run_hallucination_tests.py --mode mock

  # 2. API 模式（调用实际 CountBot API）
  python tests/hallucination/run_hallucination_tests.py --mode api --api-url http://localhost:8000

  # 3. 交互模式（逐个输入响应）
  python tests/hallucination/run_hallucination_tests.py --mode interactive

  # 4. 单用例测试
  python tests/hallucination/run_hallucination_tests.py --case factual-001
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.hallucination.test_cases import (
    ALL_TEST_CASES,
    FACTUAL_CASES,
    TOOL_CASES,
    SOURCE_CASES,
    INSTRUCTION_CASES,
    SELF_KNOWLEDGE_CASES,
    CONTEXT_CASES,
    NUMERICAL_CASES,
    HallucinationTestCase,
    HallucinationType,
    get_case_summary,
)
from tests.hallucination.evaluator import (
    HallucinationEvaluator,
    EvalReport,
    MOCK_RESPONSES,
)


async def run_mock_mode():
    """用 mock 响应运行评估器，验证框架本身工作正常"""
    print("=== 规则匹配模式：使用预定义 Mock 响应 ===\n")
    print("这演示了评估器如何检测不同质量的 AI 响应。\n")

    evaluator = HallucinationEvaluator()
    report = evaluator.evaluate_batch_sync(MOCK_RESPONSES)
    evaluator.print_report(report)

    # 导出
    report_path = PROJECT_ROOT / "tests" / "hallucination" / "eval_report_mock.json"
    evaluator.export_report_json(report, str(report_path))

    print("\n💡 提示：查看 tests/hallucination/eval_report_mock.json 获取报告 JSON")


async def run_api_mode(api_url: str):
    """
    通过 CountBot API 运行所有测试用例。

    需要 CountBot 在运行中，且 API 可访问。
    调用 POST /api/chat/send 发送测试 prompt，收集响应后评估。
    """
    import aiohttp

    evaluator = HallucinationEvaluator(api_url=api_url)

    print(f"=== API 模式：连接 {api_url} ===\n")

    responses = {}
    total = len(ALL_TEST_CASES)

    for i, case in enumerate(ALL_TEST_CASES, 1):
        print(f"[{i}/{total}] 发送: {case.id} — {case.description[:50]}...")

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "message": case.prompt,
                    "channel": "web-chat",
                }
                async with session.post(
                    f"{api_url}/api/chat/send",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        responses[case.id] = data.get("response", "")
                    else:
                        print(f"    ⚠ API 返回 {resp.status}")
                        responses[case.id] = ""
        except Exception as e:
            print(f"    ❌ 请求失败: {e}")
            responses[case.id] = ""

        # 避免请求过快
        await asyncio.sleep(0.5)

    # 评估所有响应
    print(f"\n--- 评估 {len(responses)} 个响应 ---\n")
    report = await evaluator.evaluate_batch(responses, use_llm_judge=False)
    evaluator.print_report(report)

    # 导出
    report_path = PROJECT_ROOT / "tests" / "hallucination" / "eval_report_api.json"
    evaluator.export_report_json(report, str(report_path))


async def run_interactive_mode():
    """交互模式：逐个展示测试用例，用户手动输入 AI 响应进行评估"""
    evaluator = HallucinationEvaluator()

    print("=== 交互模式 ===\n")
    print("会逐个展示测试用例，请你复制 CountBot 的实际回复粘贴进来。\n")
    print("输入 'skip' 跳过当前用例，输入 'quit' 退出。\n")

    responses = {}
    for case in ALL_TEST_CASES:
        print(f"\n{'─' * 60}")
        print(f"📋 用例: {case.id} [{case.type.value}] [{case.severity}]")
        print(f"   描述: {case.description}")
        print(f"   预期: {case.expected_behavior}")
        print(f"\n   🗣 Prompt:")
        print(f"   {case.prompt}")
        print(f"\n   请输入 CountBot 的回复（多行输入以空行结束）:")

        lines = []
        while True:
            try:
                line = input()
                if line == "" and lines:
                    break
                if line.lower() == "skip":
                    lines = []
                    break
                if line.lower() == "quit":
                    print("退出评估。")
                    return
                lines.append(line)
            except EOFError:
                break

        response = "\n".join(lines) if lines else ""
        if response.lower() == "skip":
            responses[case.id] = ""
            print("   ⏭ 已跳过")
        else:
            responses[case.id] = response
            print(f"   ✓ 已记录 ({len(response)} 字符)")

    # 评估
    if responses:
        print(f"\n--- 评估 {len([r for r in responses.values() if r])} 个响应 ---\n")
        report = evaluator.evaluate_batch_sync(responses)
        evaluator.print_report(report)

        report_path = PROJECT_ROOT / "tests" / "hallucination" / "eval_report_interactive.json"
        evaluator.export_report_json(report, str(report_path))


async def run_single_case(case_id: str):
    """运行单个测试用例的快速检查"""
    case = None
    for c in ALL_TEST_CASES:
        if c.id == case_id:
            case = c
            break

    if not case:
        print(f"未找到用例: {case_id}")
        print(f"可用用例: {', '.join(c.id for c in ALL_TEST_CASES)}")
        return

    print(f"=== 单用例检查: {case.id} ===\n")
    print(f"类型: {case.type.value}")
    print(f"严重程度: {case.severity}")
    print(f"描述: {case.description}")
    print(f"预期行为: {case.expected_behavior}")
    if case.ground_truth:
        print(f"地面真相: {case.ground_truth}")
    print(f"\n--- Prompt ---")
    print(case.prompt)
    print(f"\n--- 规则检查 ---")
    print(f"禁止模式 ({len(case.forbidden_patterns)}): {case.forbidden_patterns}")
    print(f"期望模式 ({len(case.expected_patterns)}): {case.expected_patterns}")
    print(f"\n请将 CountBot 的回复粘贴到下面（空行结束）:")

    lines = []
    while True:
        try:
            line = input()
            if line == "" and lines:
                break
            lines.append(line)
        except EOFError:
            break

    response = "\n".join(lines) if lines else ""
    if not response:
        print("未输入响应。")
        return

    evaluator = HallucinationEvaluator()
    result = evaluator.evaluate_by_rules(case, response)

    status = "✅ 通过" if result.passed else "❌ 未通过"
    print(f"\n评估结果: {status}")
    print(f"得分: {result.score:.2f}")
    print(f"原因: {result.reason}")


def print_summary():
    """打印测试用例概览"""
    print("=== CountBot 幻觉测试用例概览 ===\n")
    summary = get_case_summary()
    total = 0
    for h_type, stats in summary.items():
        total += stats["count"]
        sev_str = ", ".join(f"{k}:{v}" for k, v in stats["severity"].items() if v > 0)
        print(f"  {h_type:20s} {stats['count']:2d} 个用例  [{sev_str}]")

    print(f"\n  总计: {total} 个用例")
    print(f"\n幻觉类型说明:")
    print(f"  factual        — 编造不存在的事实/功能")
    print(f"  tool           — 调用不存在的工具或错误参数")
    print(f"  source         — 引用不存在的文档/文件/URL")
    print(f"  instruction    — 执行用户未要求的操作")
    print(f"  self_knowledge — 对自身能力的错误描述")
    print(f"  context        — 忽略或曲解上下文信息")
    print(f"  numerical      — 编造具体数字/数据")


async def main():
    parser = argparse.ArgumentParser(
        description="CountBot 幻觉测试运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --mode mock          使用 mock 响应验证评估器
  %(prog)s --mode interactive   交互式输入响应
  %(prog)s --case factual-001   快速检查单个用例
  %(prog)s --summary            查看测试用例概览
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["mock", "api", "interactive"],
        default="mock",
        help="运行模式: mock(演示) / api(调用API) / interactive(交互输入)",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="CountBot API 地址 (默认 http://localhost:8000)",
    )
    parser.add_argument(
        "--case",
        help="指定单个用例 ID 进行快速检查",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="仅打印测试用例概览",
    )

    args = parser.parse_args()

    if args.summary:
        print_summary()
        return

    if args.case:
        await run_single_case(args.case)
        return

    if args.mode == "mock":
        await run_mock_mode()
    elif args.mode == "api":
        await run_api_mode(args.api_url)
    elif args.mode == "interactive":
        await run_interactive_mode()


if __name__ == "__main__":
    asyncio.run(main())
