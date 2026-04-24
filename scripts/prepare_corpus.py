"""从 HPO + MONDO/Orphanet 本体文件生成疾病知识库 JSONL。

流程：
1. 检查 data/ontology/ 下是否存在 hp.obo 和 mondo.obo，不存在则打印下载命令并退出
2. 用 pronto 解析 obo 文件
3. 提取每个疾病的：
   - id (MONDO:xxx / OMIM:xxx / Orpha:xxx)
   - name（英文名）
   - synonyms（同义词列表）
   - definition（定义文本）
   - associated_hpo（通过 MONDO 关联的 HPO terms）
   - associated_genes（通过 xref 关联的基因符号）
   - url（monarchinitiative.org 链接）
4. 输出：
   - data/corpus/diseases.jsonl（主索引，每行一个疾病）
   - data/corpus/hpo_index.jsonl（HPO ID → 关联疾病列表，供快速反查）

幂等：already_done 文件已存在则跳过，加 --force 强制重建。

每行 JSONL 格式：
{
  "id": "MONDO:0100135",
  "name": "Dravet syndrome",
  "synonyms": ["SMEI", "Severe myoclonic epilepsy in infancy"],
  "definition": "...",
  "associated_hpo": ["HP:0002373", "HP:0001250"],
  "associated_genes": ["SCN1A"],
  "url": "https://monarchinitiative.org/disease/MONDO:0100135"
}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 HPO/MONDO obo 文件生成 diseases.jsonl BM25 索引",
    )
    parser.add_argument(
        "--ontology-dir",
        default=os.getenv("ONTOLOGY_DIR", "./data/ontology"),
    )
    parser.add_argument(
        "--corpus-dir",
        default=os.getenv("CORPUS_DIR", "./data/corpus"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使输出文件已存在也强制重新生成",
    )
    return parser.parse_args()


def check_obo_files(ontology_dir: Path) -> tuple[Path, Path]:
    """检查 obo 文件是否存在，不存在时打印下载命令并退出。"""
    hp_obo = ontology_dir / "hp.obo"
    mondo_obo = ontology_dir / "mondo.obo"

    missing = [p for p in [hp_obo, mondo_obo] if not p.exists()]
    if missing:
        print("[ERROR] 缺少本体文件，请先下载：\n")
        print("  mkdir -p data/ontology")
        print("  # HPO（约 30MB）")
        print("  curl -L https://purl.obolibrary.org/obo/hp.obo -o data/ontology/hp.obo")
        print("  # MONDO（约 150MB）")
        print("  curl -L https://purl.obolibrary.org/obo/mondo.obo -o data/ontology/mondo.obo")
        sys.exit(1)

    return hp_obo, mondo_obo


def extract_xref_id(xrefs, prefix: str) -> str | None:
    """从 term.xrefs 中提取指定前缀的 ID（如 'OMIM:' / 'Orphanet:'）。"""
    for xref in xrefs:
        xid = str(xref.id) if hasattr(xref, "id") else str(xref)
        if xid.startswith(prefix):
            return xid
    return None


def build_disease_records(hp_obo: Path, mondo_obo: Path) -> list[dict]:
    """解析 MONDO + HPO obo，返回疾病记录列表。"""
    try:
        import pronto  # type: ignore[import]
    except ImportError:
        print("[ERROR] pronto 未安装: pip install pronto>=2.5")
        sys.exit(1)

    print(f"[INFO] 加载 HPO: {hp_obo}")
    hpo_ont = pronto.Ontology(str(hp_obo))
    # HP:id → name 映射
    hpo_names: dict[str, str] = {
        str(t.id): t.name or str(t.id)
        for t in hpo_ont.terms()
        if not t.obsolete
    }
    print(f"[INFO] HPO 条目: {len(hpo_names)}")

    print(f"[INFO] 加载 MONDO（约需 30-60s）: {mondo_obo}")
    mondo_ont = pronto.Ontology(str(mondo_obo))

    records: list[dict] = []
    skipped = 0

    for term in mondo_ont.terms():
        if term.obsolete:
            skipped += 1
            continue

        # 过滤：只保留有 Orphanet 或 OMIM xref 的罕见病
        xrefs = list(getattr(term, "xrefs", []))
        orpha_id = extract_xref_id(xrefs, "Orphanet:")
        omim_id = extract_xref_id(xrefs, "OMIM:")

        if not orpha_id and not omim_id:
            skipped += 1
            continue

        # 规范化疾病 ID
        if orpha_id:
            disease_id = orpha_id.replace("Orphanet:", "ORPHA:")
        else:
            disease_id = omim_id

        # 提取同义词
        synonyms = [str(s.description) for s in getattr(term, "synonyms", [])][:5]

        # 提取关联 HPO（通过 relationships["has phenotype"]）
        associated_hpo: list[str] = []
        for rel_type, related_terms in getattr(term, "relationships", {}).items():
            rel_name = str(rel_type).lower()
            if "phenotype" in rel_name or "has_phenotype" in rel_name:
                for rt in related_terms:
                    tid = str(rt.id)
                    if tid.startswith("HP:"):
                        associated_hpo.append(tid)

        # 提取关联基因（来自 xref RO: 基因关系或 HGNC xref）
        associated_genes: list[str] = []
        for xref in xrefs:
            xid = str(xref.id) if hasattr(xref, "id") else str(xref)
            if xid.startswith("HGNC:"):
                # HGNC xref 中有时包含基因符号
                desc = str(getattr(xref, "description", "") or "")
                if desc:
                    associated_genes.append(desc.split()[0])

        url = f"https://monarchinitiative.org/disease/{term.id}"

        records.append({
            "id": str(term.id),
            "name": term.name or str(term.id),
            "synonyms": synonyms,
            "definition": str(term.definition or "")[:500],
            "associated_hpo": list(dict.fromkeys(associated_hpo))[:20],
            "associated_genes": list(dict.fromkeys(associated_genes))[:10],
            "url": url,
            # 冗余字段，兼容旧版 rag.py
            "description": str(term.definition or "")[:300],
            "source": "Orphanet" if orpha_id else "OMIM",
        })

    print(f"[INFO] 提取疾病条目: {len(records)}（跳过: {skipped}）")
    return records


def build_hpo_index(records: list[dict]) -> dict[str, list[str]]:
    """构建 HPO ID → 关联疾病 ID 列表的反向索引。"""
    index: dict[str, list[str]] = defaultdict(list)
    for rec in records:
        for hpo_id in rec.get("associated_hpo", []):
            index[hpo_id].append(rec["id"])
    return dict(index)


def write_jsonl(data: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"[SUCCESS] {len(data)} 条 → {path}")


def main() -> None:
    args = parse_args()
    ontology_dir = Path(args.ontology_dir)
    corpus_dir = Path(args.corpus_dir)

    diseases_path = corpus_dir / "diseases.jsonl"
    hpo_index_path = corpus_dir / "hpo_index.jsonl"

    # 幂等检查：已生成则跳过（除非 --force）
    if diseases_path.exists() and hpo_index_path.exists() and not args.force:
        print(f"[INFO] 语料库已存在，跳过生成（使用 --force 强制重建）：")
        print(f"  {diseases_path}")
        print(f"  {hpo_index_path}")
        sys.exit(0)

    hp_obo, mondo_obo = check_obo_files(ontology_dir)
    records = build_disease_records(hp_obo, mondo_obo)

    if not records:
        print("[ERROR] 未提取到任何疾病记录，请检查 obo 文件格式")
        sys.exit(1)

    write_jsonl(records, diseases_path)

    hpo_index = build_hpo_index(records)
    hpo_rows = [{"hpo_id": k, "disease_ids": v} for k, v in hpo_index.items()]
    write_jsonl(hpo_rows, hpo_index_path)

    print("\n[NEXT] 运行诊断:")
    print("  python main.py data/samples/mock_case.json")
    print("  streamlit run app.py")


if __name__ == "__main__":
    main()
