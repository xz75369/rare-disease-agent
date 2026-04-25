"""RAG 检索层：HPO JAX API（疾病-表型）+ PubMed + ClinVar + gnomAD 四路在线检索。

设计原则：
- 无本地知识库依赖，所有数据均来自权威外部 API
- 各路 API 超时或不可用时静默跳过，流程不中断
- 在线查询仅含医学术语与基因符号，绝不含患者个人信息
- Evidence snippet ≤250 字（与 schemas 一致）
"""
from __future__ import annotations

import os
import re
from typing import Optional

import httpx
from dotenv import load_dotenv

from external_db import (
    fetch_clinvar_gnomad_for_input,
    hpo_disease_lookup,
    pubmed_search,
)
from schemas import DiagnosisInput, Evidence

load_dotenv()


def build_search_queries(inp: DiagnosisInput) -> list[str]:
    """根据 DiagnosisInput 构造 PubMed 检索 query 列表。

    Returns:
        去重后的 query 列表（用于 PubMed 检索）
    """
    queries: list[str] = []

    # Query 类型 1：HPO 驱动
    if inp.hpo_terms:
        hpo_names = [h.name for h in inp.hpo_terms[:5]]
        queries.append(" ".join(hpo_names) + " rare disease diagnosis")

    # Query 类型 2：基因驱动（candidate_variants）
    for var in inp.candidate_variants[:3]:
        queries.append(f"{var.gene} neurodevelopmental disorder phenotype")
        if var.hgvs_p:
            queries.append(f"{var.gene} {var.hgvs_p} pathogenicity")
        if var.acmg_class in ("VUS", "LP"):
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

    return list(dict.fromkeys(queries))


async def gather_evidence(
    queries: list[str],
    existing_urls: Optional[set[str]] = None,
    start_ref_id: int = 1,
    inp: Optional[DiagnosisInput] = None,
) -> list[Evidence]:
    """主入口：HPO API + PubMed + ClinVar + gnomAD 四路检索，去重并转为 Evidence。

    Args:
        queries: PubMed 检索查询字符串列表
        existing_urls: 已有证据的 URL 集合，用于去重
        start_ref_id: ref_id 起始编号
        inp: 病例输入，用于 HPO API 查询和 ClinVar/gnomAD 变异检索

    Returns:
        新增的 Evidence 列表
    """
    existing_urls = existing_urls or set()
    seen_urls: set[str] = set(existing_urls)
    results: list[Evidence] = []
    ref_id = start_ref_id

    enable_hpo = os.getenv("ENABLE_HPO_API", "true").lower() == "true"
    enable_pubmed = os.getenv("ENABLE_PUBMED", "true").lower() == "true"
    pubmed_timeout = float(os.getenv("PUBMED_TIMEOUT", "20"))
    pubmed_max = int(os.getenv("PUBMED_MAX_RESULTS", "25"))
    pubmed_query_limit = int(os.getenv("RAG_PUBMED_MAX_QUERIES", "3"))

    headers = {"User-Agent": os.getenv("HTTP_USER_AGENT", "rare-disease-agent/0.1")}
    timeout = httpx.Timeout(pubmed_timeout, connect=15.0)

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:

            # ── 1. HPO JAX API（疾病-表型关联，替代本地 BM25）─────────────────
            if enable_hpo and inp is not None and inp.hpo_terms:
                print(f"  [RAG] HPO API 查询（{len(inp.hpo_terms)} 个术语）…")
                try:
                    hpo_evs = await hpo_disease_lookup(
                        client, inp.hpo_terms, seen_urls, ref_id
                    )
                    for ev in hpo_evs:
                        ev.ref_id = ref_id
                        ref_id += 1
                    results.extend(hpo_evs)
                    print(f"  [RAG] HPO API 完成（+{len(hpo_evs)} 条）")
                except Exception as e:
                    print(f"  [HPO API] 检索失败: {e}")

            # ── 2. PubMed（文献检索）──────────────────────────────────────────
            for qi, query in enumerate(queries):
                if not enable_pubmed or qi >= pubmed_query_limit:
                    if enable_pubmed and qi >= pubmed_query_limit:
                        print(
                            f"  [RAG] query {qi + 1}: 跳过 PubMed"
                            f"（仅前 {pubmed_query_limit} 条 query，"
                            f"可调大 RAG_PUBMED_MAX_QUERIES）"
                        )
                    continue
                print(f"  [RAG] query {qi + 1}/{len(queries)}: PubMed（retmax={pubmed_max}）…")
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
                print(f"  [RAG] query {qi + 1}: PubMed 完成（{len(pubmed_results)} 条）")

            # ── 3. ClinVar + gnomAD（变异级别证据）───────────────────────────
            if inp is not None:
                try:
                    print("  [RAG] ClinVar / gnomAD 检索开始…")
                    extra = await fetch_clinvar_gnomad_for_input(
                        client, inp, seen_urls, ref_id
                    )
                    results.extend(extra)
                    print(f"  [RAG] ClinVar / gnomAD 完成（+{len(extra)} 条）")
                except Exception as e:
                    print(f"  [ClinVar/gnomAD] 检索失败: {e}")

    except Exception as e:
        print(f"  [gather_evidence] HTTP 客户端异常: {e}")

    return results
