"""RAG 检索层：本地 BM25 + 可选 PubMed / ClinVar / gnomAD 在线检索。

设计原则：
- 本地 KB 是主力，即使断网也能运行核心 4 步流程
- PubMed / ClinVar / gnomAD 为补充，超时或不可用时静默跳过
- 在线查询仅含医学术语与基因符号，绝不含患者个人信息
- Evidence snippet ≤250 字（与 schemas 一致）
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from rank_bm25 import BM25Okapi

from external_db import fetch_clinvar_gnomad_for_input, pubmed_search
from schemas import DiagnosisInput, Evidence

load_dotenv()


class LocalKBRetriever:
    """基于 BM25Okapi 的本地疾病知识库检索器。

    知识库来源：scripts/prepare_corpus.py 生成的 data/corpus/diseases.jsonl。
    每行格式：{"id":..., "name":..., "definition":..., "associated_hpo":[...], "url":...}
    """

    def __init__(self, corpus_dir: Optional[str] = None) -> None:
        self.corpus_dir = Path(corpus_dir or os.getenv("CORPUS_DIR", "./data/corpus"))
        self.documents: list[dict] = []
        self.bm25: Optional[BM25Okapi] = None
        self._loaded = False

    def load(self) -> None:
        """加载 corpus 目录下所有 *.jsonl 并构建 BM25 索引（首次调用时执行，此后幂等）。

        优先加载 diseases.jsonl（基础疾病库），再加载其余 *.jsonl（如 acmg_genes.jsonl）。
        """
        if self._loaded:
            return

        base_path = self.corpus_dir / "diseases.jsonl"
        if not base_path.exists():
            raise FileNotFoundError(
                f"Corpus not found: {base_path}\n"
                "Run: python scripts/prepare_corpus.py"
            )

        self.documents = []

        # 按文件名排序加载，diseases.jsonl 首先，其余补充库追加
        jsonl_files = sorted(self.corpus_dir.glob("*.jsonl"))
        for jsonl_path in jsonl_files:
            count_before = len(self.documents)
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.documents.append(json.loads(line))
            added = len(self.documents) - count_before
            print(f"  [LocalKB] {jsonl_path.name}: +{added} 条")

        # 每个文档：name + synonyms + definition + HPO IDs + ACMG evidence 拼接为检索文本
        tokenized_corpus = [self._tokenize(self._doc_text(doc)) for doc in self.documents]
        self.bm25 = BM25Okapi(tokenized_corpus)
        self._loaded = True
        print(f"  [LocalKB] 共加载 {len(self.documents)} 条记录")

    def _doc_text(self, doc: dict) -> str:
        """将文档字段拼接为检索用文本字符串（含 ACMG evidence 字段）。"""
        acmg = doc.get("acmg_evidence", {})
        acmg_text = " ".join(str(v) for v in acmg.values()) if isinstance(acmg, dict) else ""
        parts = [
            doc.get("name", ""),
            " ".join(doc.get("synonyms", [])),
            doc.get("definition", ""),
            " ".join(doc.get("associated_hpo", [])),
            " ".join(doc.get("associated_genes", [])),
            acmg_text,
        ]
        return " ".join(p for p in parts if p)

    def _tokenize(self, text: str) -> list[str]:
        """提取字母数字及冒号（保留 HP:0001250 格式），转小写。"""
        return re.findall(r"[A-Za-z0-9:]+", text.lower())

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25 检索，返回 top_k 条文档（仅 score > 0 的结果）。

        Args:
            query: 检索查询（HPO 名称 + 基因名 + 自由文本）
            top_k: 返回上限
        """
        if not self._loaded:
            self.load()

        tokens = self._tokenize(query)
        scores = self.bm25.get_scores(tokens)
        top_indices = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )[:top_k]

        return [
            {**self.documents[idx], "_score": float(scores[idx])}
            for idx in top_indices
            if scores[idx] > 0
        ]


def build_search_queries(inp) -> list[str]:
    """根据 DiagnosisInput 构造多条检索 query。
    支持两种场景：Exomiser hits / 临床候选变异。

    Args:
        inp: DiagnosisInput 实例

    Returns:
        去重后的 query 列表
    """
    queries = []

    # Query 类型 1：HPO 驱动（所有场景通用）
    if inp.hpo_terms:
        hpo_names = [h.name for h in inp.hpo_terms[:5]]
        queries.append(" ".join(hpo_names) + " rare disease diagnosis")

    # Query 类型 2：基因驱动（针对 candidate_variants）
    for var in inp.candidate_variants[:3]:
        queries.append(f"{var.gene} neurodevelopmental disorder phenotype")
        if var.hgvs_p:
            queries.append(f"{var.gene} {var.hgvs_p} pathogenicity")
        # VUS/LP 额外检索功能验证相关
        if var.acmg_class in ["VUS", "LP"]:
            queries.append(f"{var.gene} functional study de novo")

    # Query 类型 3：Exomiser top 基因
    for hit in inp.exomiser_hits[:3]:
        queries.append(f"{hit.gene_symbol} phenotype clinical features")

    # Query 类型 4：关联疾病名
    disease_names: set[str] = set()
    for var in inp.candidate_variants:
        disease_names.update(var.associated_diseases)
    for hit in inp.exomiser_hits[:3]:
        for d in hit.associated_diseases:
            if isinstance(d, dict) and d.get("disease_name"):
                disease_names.add(d["disease_name"])
            elif isinstance(d, str):
                disease_names.add(d)
    for name in list(disease_names)[:3]:
        queries.append(f"{name} clinical diagnosis criteria")

    # 保持顺序去重
    return list(dict.fromkeys(queries))


async def gather_evidence(
    queries: list[str],
    kb: LocalKBRetriever,
    existing_urls: set[str] | None = None,
    start_ref_id: int = 1,
    inp: DiagnosisInput | None = None,
) -> list[Evidence]:
    """主入口：本地 BM25 + 可选 PubMed + ClinVar + gnomAD，去重并转为 Evidence。

    Args:
        queries: 检索查询字符串列表
        kb: 已初始化的 LocalKBRetriever 实例
        existing_urls: 已有证据的 URL 集合，用于去重
        start_ref_id: ref_id 起始编号（续接已有证据；最终由 Agent 重新编号）
        inp: 若提供，则基于候选变异 / Exomiser 基因拉取 ClinVar 与 gnomAD

    Returns:
        新增的 Evidence 列表（不含 existing_urls 中已有的条目）
    """
    existing_urls = existing_urls or set()
    seen_urls: set[str] = set(existing_urls)
    results: list[Evidence] = []
    ref_id = start_ref_id

    enable_pubmed = os.getenv("ENABLE_PUBMED", "true").lower() == "true"
    pubmed_timeout = float(os.getenv("PUBMED_TIMEOUT", "20"))
    pubmed_max = int(os.getenv("PUBMED_MAX_RESULTS", "25"))

    headers = {"User-Agent": os.getenv("HTTP_USER_AGENT", "rare-disease-agent/0.1")}
    timeout = httpx.Timeout(pubmed_timeout, connect=15.0)

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            for query in queries:
                # ── 本地 BM25 ─────────────────────────────────────────────
                local_docs = kb.search(query, top_k=5)
                for doc in local_docs:
                    url = doc.get("url") or f"https://monarchinitiative.org/disease/{doc.get('id', '')}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    hpo_list = doc.get("associated_hpo", [])[:5]
                    gene_list = doc.get("associated_genes", [])[:3]
                    snippet = (
                        f"{doc.get('definition', '') or doc.get('description', '')} "
                        f"Genes:{','.join(gene_list)} HPO:{' '.join(hpo_list)}"
                    )[:250]

                    results.append(
                        Evidence(
                            ref_id=ref_id,
                            source="local_kb",
                            title=doc.get("name", "Unknown Disease"),
                            url=url,
                            snippet=snippet,
                        )
                    )
                    ref_id += 1

                # ── PubMed（NCBI E-utilities，全库可查）────────────────────
                if enable_pubmed:
                    try:
                        pubmed_results = await pubmed_search(
                            client, query=query, max_results=pubmed_max
                        )
                    except Exception as e:
                        print(f"  [PubMed] 检索失败: {e}")
                        pubmed_results = []
                    for item in pubmed_results:
                        url = item["url"]
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        results.append(
                            Evidence(
                                ref_id=ref_id,
                                source="pubmed",
                                title=item["title"],
                                url=url,
                                snippet=f"PMID:{item['pmid']} ({item['pubdate']})"[:250],
                            )
                        )
                        ref_id += 1

            # ── ClinVar + gnomAD（依赖病例中的基因 / HGVS）──────────────
            if inp is not None:
                try:
                    extra = await fetch_clinvar_gnomad_for_input(
                        client, inp, seen_urls, ref_id
                    )
                    results.extend(extra)
                except Exception as e:
                    print(f"  [ClinVar/gnomAD] 检索失败: {e}")

    except Exception as e:
        print(f"  [gather_evidence] HTTP 客户端异常: {e}")

    return results
