"""命令行运行单个病例。

用法：
    python main.py data/samples/mock_case.json
    python main.py data/samples/mock_case.json --output outputs/result.json

前置条件：
    1. vllm server 已启动（bash scripts/start_vllm.sh）
    2. 知识库已准备（python scripts/prepare_corpus.py）
    3. .env 已配置（cp .env.example .env）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent import DiagnosisAgent
from rag import LocalKBRetriever
from schemas import DiagnosisInput


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="罕见病诊断 Agent — 命令行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py data/samples/mock_case.json\n"
            "  python main.py data/samples/mock_case.json -o outputs/result.json"
        ),
    )
    parser.add_argument("case_file", help="病例 JSON 文件路径")
    parser.add_argument(
        "--output", "-o",
        help="完整 DiagnosisOutput JSON 输出路径（默认：outputs/{patient_id}.json）",
    )
    return parser.parse_args()


async def run(case_file: Path, output_path: Path | None) -> None:
    # 1. 读取输入 JSON → DiagnosisInput
    with open(case_file, encoding="utf-8") as f:
        raw = json.load(f)
    try:
        inp = DiagnosisInput.model_validate(raw)
    except Exception as e:
        print(f"[ERROR] 病例 JSON 格式错误: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 患者 ID: {inp.patient_id}")
    print(f"[INFO] HPO: {len(inp.hpo_terms)} 条  Exomiser: {len(inp.exomiser_hits)} 条")
    print()

    # 2. 初始化 LocalKBRetriever
    kb = LocalKBRetriever()
    try:
        kb.load()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # 3. 创建 DiagnosisAgent 并运行
    agent = DiagnosisAgent(kb_retriever=kb)
    output = await agent.diagnose(inp)

    # 4. 打印报告到终端
    print()
    print("=" * 70)
    print(output.report_markdown)
    print("=" * 70)

    # 5. 保存完整 DiagnosisOutput
    if output_path is None:
        Path("outputs").mkdir(exist_ok=True)
        output_path = Path("outputs") / f"{inp.patient_id}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output.model_dump(), f, ensure_ascii=False, indent=2)
    print(f"\n[SUCCESS] 完整输出已保存: {output_path}")


def main() -> None:
    args = parse_args()
    case_file = Path(args.case_file)

    if not case_file.exists():
        print(f"[ERROR] 文件不存在: {case_file}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else None
    asyncio.run(run(case_file, output_path))


if __name__ == "__main__":
    main()
