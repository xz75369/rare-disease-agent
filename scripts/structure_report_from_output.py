"""从 DiagnosisOutput JSON 的 report_markdown 解析出结构化报告段落，写入新 JSON。

用法：
    python scripts/structure_report_from_output.py outputs/P002_result.json
    python scripts/structure_report_from_output.py outputs/P002_result.json -o outputs/P002_report_structured.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _strip_title_prefix(md: str) -> str:
    """去掉开头的 '# 诊断分析报告' 标题行。"""
    lines = md.strip().splitlines()
    if lines and lines[0].startswith("# "):
        return "\n".join(lines[1:]).lstrip("\n")
    return md.strip()


def split_level2_sections(body: str) -> dict[str, str]:
    """按 ## 二级标题切分。"""
    body = body.strip()
    if not body:
        return {}
    parts = re.split(r"\n(?=## )", body)
    out: dict[str, str] = {}
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"##\s+(.+?)\n", p, re.DOTALL)
        if not m:
            continue
        title = m.group(1).strip()
        content = p[m.end() :].strip()
        out[title] = content
    return out


def _bullet_items(block: str) -> list[str]:
    """提取 #### 小节下以 '- ' 开头的条目（到下一个 #### 或 ### 或 ## 为止）。"""
    items: list[str] = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items


def parse_candidate_block(chunk: str) -> dict | None:
    """解析单个 ### Rank N: ... 块。"""
    chunk = chunk.strip()
    m = re.match(
        r"###\s+Rank\s+(\d+):\s*(.+?)\s*\n",
        chunk,
        re.DOTALL,
    )
    if not m:
        return None
    rank = int(m.group(1))
    title_line = m.group(2).strip()
    rest = chunk[m.end() :].strip()

    subsections: dict[str, str] = {}
    # #### 支持证据 / 需注意 / VUS 升级路径 / 建议下一步
    for sm in re.finditer(
        r"####\s+([^\n]+)\n(.*?)(?=\n#### |\n### |\n## |\Z)",
        rest,
        re.DOTALL,
    ):
        name = sm.group(1).strip()
        subsections[name] = sm.group(2).strip()

    gene_line = ""
    conf_line = ""
    for line in rest.splitlines():
        if line.strip().startswith("**相关基因**"):
            gene_line = line.strip()
        if line.strip().startswith("**置信度**"):
            conf_line = line.strip()

    def _vus_body() -> str:
        for k, v in subsections.items():
            if "升级路径" in k and "VUS" in k.replace(" ", ""):
                return v
        return ""

    return {
        "rank": rank,
        "title_line": title_line,
        "gene_variant_acmg_line": gene_line,
        "confidence_line": conf_line,
        "supporting_evidence": _bullet_items(subsections.get("支持证据", "")),
        "caveats": _bullet_items(subsections.get("需注意", "")),
        "vus_upgrade_path": _bullet_items(_vus_body()),
        "next_steps": _bullet_items(subsections.get("建议下一步", "")),
    }


def parse_candidates_section(text: str) -> list[dict]:
    parts = re.split(r"\n(?=### Rank \d+:)", text.strip())
    out: list[dict] = []
    for p in parts:
        p = p.strip()
        if p.startswith("### Rank"):
            parsed = parse_candidate_block(p)
            if parsed:
                out.append(parsed)
    return out


def parse_references_block(text: str) -> list[str]:
    """按行收集 [N] 开头的参考文献条目。"""
    refs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\[\d+\]\s", line):
            refs.append(line)
    return refs


def structure_from_output(data: dict) -> dict:
    md = data.get("report_markdown") or ""
    body = _strip_title_prefix(md)
    sections = split_level2_sections(body)

    candidates_raw = sections.get("候选诊断对比", "")
    structured = {
        "patient_id": data.get("patient_id"),
        "source_file_hint": "parsed_from_report_markdown",
        "clinical_summary": sections.get("临床摘要", "").strip(),
        "core_question": sections.get("核心问题", "").strip(),
        "candidate_diagnostics": parse_candidates_section(candidates_raw),
        "integrated_recommendation": sections.get("综合建议", "").strip(),
        "references": parse_references_block(sections.get("参考文献", "")),
        "hypotheses": data.get("hypotheses", []),
        "meta": data.get("meta", {}),
    }
    return structured


def main() -> None:
    ap = argparse.ArgumentParser(description="从 Agent 输出 JSON 提取结构化诊断报告")
    ap.add_argument("input_json", type=Path, help="含 report_markdown 的完整输出 JSON")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        help="结构化 JSON 输出路径（默认：与输入同目录，文件名 *_report_structured.json）",
    )
    args = ap.parse_args()
    inp = args.input_json
    if not inp.exists():
        print(f"[ERROR] 文件不存在: {inp}", file=sys.stderr)
        sys.exit(1)
    with open(inp, encoding="utf-8") as f:
        data = json.load(f)

    out_obj = structure_from_output(data)

    out_path = args.output
    if out_path is None:
        stem = inp.stem.replace("_result", "").replace(".json", "")
        out_path = inp.parent / f"{stem}_report_structured.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_obj, f, ensure_ascii=False, indent=2)
    print(f"[SUCCESS] 已写入: {out_path}")


if __name__ == "__main__":
    main()
