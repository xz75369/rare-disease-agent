"""PubMed / ClinVar（NCBI E-utilities）与 gnomAD（GraphQL）在线检索。

- 仅发送医学术语与基因符号，不包含患者身份信息。
- 支持 NCBI API Key 提升 E-utilities 限额；建议设置 NCBI_EMAIL。
- gnomAD 请求带退避重试，降低 429 概率。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

import httpx

from schemas import DiagnosisInput, Evidence

NCBI_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GNOMAD_API_DEFAULT = "https://gnomad.broadinstitute.org/api"
GNOMAD_ALLOWED_DATASETS = frozenset({"gnomad_r4", "gnomad_r3", "gnomad_r2_1"})
HPO_API_BASE = "https://hpo.jax.org/api/hpo"


def _user_agent() -> str:
    mail = os.getenv("NCBI_EMAIL", "anonymous@example.com").strip()
    tool = os.getenv("NCBI_TOOL", "rare_disease_agent").strip()
    return f"{tool}/0.1 (mailto:{mail})"


def ncbi_query_params() -> dict[str, str]:
    """NCBI 推荐：tool + email；有 API Key 时并入以提升速率上限。"""
    p: dict[str, str] = {
        "tool": os.getenv("NCBI_TOOL", "rare_disease_agent"),
        "email": os.getenv("NCBI_EMAIL", "anonymous@example.com"),
    }
    key = os.getenv("NCBI_API_KEY", "").strip()
    if key:
        p["api_key"] = key
    return p


def pubmed_compose_term(user_query: str) -> str:
    """PubMed 检索式：默认可查全库；若设置 PUBMED_DATE_FILTER 则追加 AND 子句。"""
    extra = os.getenv("PUBMED_DATE_FILTER", "").strip()
    if extra:
        return f"({user_query}) AND ({extra})"
    return user_query


def nc_accession_to_chrom(nc: str) -> Optional[str]:
    """NC_000001.11 → 1 … NC_000023.11 → X（线粒体 NC_012920 返回 None，避免错误拼接）。"""
    m = re.match(r"^NC_0*(\d+)\.\d+$", nc.strip())
    if not m:
        return None
    n = int(m.group(1))
    if 1 <= n <= 22:
        return str(n)
    if n == 23:
        return "X"
    if n == 24:
        return "Y"
    if n >= 12920:  # mitochondrial NC_012920 etc.
        return None
    return None


def clinvar_record_to_gnomad_variant_id(rec: dict) -> Optional[str]:
    """从 ClinVar esummary 的 variation_set 推断 gnomAD variantId（chrom-pos-ref-alt）。"""
    vs = rec.get("variation_set")
    if not vs or not isinstance(vs, list):
        return None
    block = vs[0] if isinstance(vs[0], dict) else None
    if not block:
        return None

    spdi = (block.get("canonical_spdi") or "").strip()
    if spdi.startswith("NC_"):
        parts = spdi.split(":")
        if len(parts) == 4:
            nc, pos, ref, alt = parts
            chrom = nc_accession_to_chrom(nc)
            if not chrom:
                return None
            ref = (ref or "").upper()
            alt = (alt or "").upper()
            if ref == ".":
                ref = ""
            if alt == ".":
                alt = ""
            if not pos.isdigit():
                return None
            return f"{chrom}-{pos}-{ref}-{alt}"

    locs = block.get("variation_loc") or []
    for L in locs:
        if not isinstance(L, dict):
            continue
        if L.get("assembly_name") != "GRCh38":
            continue
        chrom = str(L.get("chr") or "").strip()
        if chrom.upper() in ("23", "CHR23"):
            chrom = "X"
        elif chrom.upper() in ("24", "CHR24"):
            chrom = "Y"
        pos = L.get("display_start") or L.get("start")
        ref = (L.get("ref") or "").upper()
        alt = (L.get("alt") or "").upper()
        if chrom and pos and ref and alt:
            return f"{chrom}-{pos}-{ref}-{alt}"
    return None


def _clinvar_title(rec: dict) -> str:
    return (rec.get("title") or rec.get("variation_set_name") or "ClinVar record")[:220]


def _clinvar_snippet(rec: dict) -> str:
    g = rec.get("germline_classification") or {}
    if isinstance(g, dict):
        parts = [
            str(g.get("description", "")),
            str(g.get("review_status", "")),
        ]
    else:
        parts = []
    mcs = rec.get("molecular_consequence_list") or []
    if isinstance(mcs, list) and mcs:
        parts.append("Molecular:" + ",".join(str(x) for x in mcs[:3]))
    return " | ".join(p for p in parts if p)[:250]


def iter_clinvar_gene_targets(inp: DiagnosisInput) -> list[tuple[str, Optional[str]]]:
    """(基因符号, 可选 HGVS 提示) 用于 ClinVar 检索与结果过滤。"""
    rows: list[tuple[str, Optional[str]]] = []
    for var in inp.candidate_variants[:6]:
        hint = " ".join(x for x in (var.hgvs_c, var.hgvs_p or "") if x).strip() or None
        rows.append((var.gene.strip(), hint))
    for hit in inp.exomiser_hits[:4]:
        g = (hit.gene_symbol or "").strip()
        if not g:
            continue
        h = (hit.variant_hgvs or "").strip() or None
        rows.append((g, h))
    seen: set[str] = set()
    out: list[tuple[str, Optional[str]]] = []
    for g, h in rows:
        if g in seen:
            continue
        seen.add(g)
        out.append((g, h))
    return out[:10]


async def _sleep_backoff(attempt: int) -> None:
    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))


async def ncbi_get_json(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, Any],
    max_retries: int = 4,
) -> Optional[dict]:
    merged = {**params, **ncbi_query_params()}
    for attempt in range(max_retries):
        try:
            r = await client.get(f"{NCBI_EUTILS}/{endpoint}", params=merged)
            if r.status_code == 429:
                await _sleep_backoff(attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  [NCBI] {endpoint} 失败: {e}")
                return None
            await _sleep_backoff(attempt)
    return None


async def pubmed_search(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
) -> list[dict]:
    """PubMed：esearch + esummary，返回 pmid/title/pubdate/url。"""
    term = pubmed_compose_term(query)
    data = await ncbi_get_json(
        client,
        "esearch.fcgi",
        {
            "db": "pubmed",
            "term": term,
            "retmax": max_results,
            "retmode": "json",
            "sort": "relevance",
        },
    )
    if not data:
        return []
    id_list: list[str] = data.get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    sm = await ncbi_get_json(
        client,
        "esummary.fcgi",
        {
            "db": "pubmed",
            "id": ",".join(id_list),
            "retmode": "json",
        },
    )
    if not sm:
        return []
    summaries = sm.get("result", {})
    results = []
    for pmid in id_list:
        if pmid in summaries and isinstance(summaries[pmid], dict):
            item = summaries[pmid]
            results.append(
                {
                    "pmid": pmid,
                    "title": (item.get("title") or "")[:200],
                    "pubdate": item.get("pubdate", ""),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                }
            )
    return results


async def clinvar_gene_summaries(
    client: httpx.AsyncClient,
    gene: str,
    hgvs_hint: Optional[str],
    esearch_retmax: int,
    evidence_cap: int,
) -> list[tuple[str, dict]]:
    """ClinVar：按基因名 esearch → esummary；若有 HGVS 提示则优先保留标题/字段匹配的条目。"""
    term = f"{gene}[Gene Name]"
    data = await ncbi_get_json(
        client,
        "esearch.fcgi",
        {
            "db": "clinvar",
            "term": term,
            "retmax": esearch_retmax,
            "retmode": "json",
            "sort": "relevance",
        },
    )
    if not data:
        return []
    id_list: list[str] = data.get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    cap_ids = int(os.getenv("CLINVAR_ESUMMARY_MAX_IDS", "30"))
    id_list = id_list[: max(1, cap_ids)]

    sm = await ncbi_get_json(
        client,
        "esummary.fcgi",
        {
            "db": "clinvar",
            "id": ",".join(id_list),
            "retmode": "json",
        },
    )
    if not sm:
        return []
    result = sm.get("result", {})
    uids = result.get("uids") or []
    pairs: list[tuple[str, dict]] = []
    for uid in uids:
        rec = result.get(uid)
        if isinstance(rec, dict):
            pairs.append((str(uid), rec))

    if hgvs_hint:
        hint_l = hgvs_hint.lower()
        # 取 c. 或 p. 片段增强匹配（避免整段 NM 过长）
        tokens = [t for t in re.findall(r"[cp]\.[A-Za-z0-9_>]+", hint_l)]
        if not tokens:
            tokens = [hint_l]

        def score(pair: tuple[str, dict]) -> int:
            blob = json.dumps(pair[1], ensure_ascii=False).lower()
            t0 = pair[1].get("title", "").lower()
            s = 0
            for t in tokens:
                if t in t0:
                    s += 3
                if t in blob:
                    s += 1
            return s

        pairs.sort(key=score, reverse=True)
        pairs = [p for p in pairs if score(p) > 0][:evidence_cap]
        if not pairs:
            pairs = [(str(uid), result[str(uid)]) for uid in uids[:evidence_cap] if str(uid) in result]
    else:
        pairs = pairs[:evidence_cap]

    return pairs


async def gnomad_variant_lookup(
    client: httpx.AsyncClient,
    variant_id: str,
    dataset: str,
) -> Optional[dict]:
    """gnomAD GraphQL variant(variantId, dataset)，成功时返回 data.variant 字典。"""
    if dataset not in GNOMAD_ALLOWED_DATASETS:
        dataset = "gnomad_r4"
    url = os.getenv("GNOMAD_API_URL", GNOMAD_API_DEFAULT).rstrip("/")
    # DatasetId 以字面量嵌入（白名单），避免注入
    q = (
        "query { variant(variantId: "
        + json.dumps(variant_id)
        + f", dataset: {dataset}) "
        + "{ variant_id joint { ac an homozygote_count } "
        + "exome { ac an } genome { ac an } } }"
    )
    body = {"query": q}
    headers = {"User-Agent": _user_agent(), "Content-Type": "application/json"}
    for attempt in range(6):
        try:
            r = await client.post(url, json=body, headers=headers)
            if r.status_code == 429:
                await _sleep_backoff(attempt)
                continue
            r.raise_for_status()
            payload = r.json()
            v = (payload.get("data") or {}).get("variant")
            if isinstance(v, dict):
                return v
            return None
        except Exception as e:
            if attempt == 5:
                print(f"  [gnomAD] 查询失败 {variant_id!r}: {e}")
                return None
            await _sleep_backoff(attempt)
    return None


def _fmt_af(ac: Optional[int], an: Optional[int]) -> str:
    if ac is None or an in (None, 0):
        return "n/a"
    try:
        return f"{float(ac) / float(an):.4e}"
    except Exception:
        return "n/a"


def gnomad_variant_to_snippet(variant: dict) -> str:
    j = variant.get("joint") or {}
    ex = variant.get("exome") or {}
    ge = variant.get("genome") or {}
    parts = [
        f"joint AC={j.get('ac')} AN={j.get('an')} AF={_fmt_af(j.get('ac'), j.get('an'))} Hom={j.get('homozygote_count')}",
        f"exome AC={ex.get('ac')} AN={ex.get('an')}",
        f"genome AC={ge.get('ac')} AN={ge.get('an')}",
    ]
    return " | ".join(parts)[:250]


async def hpo_disease_lookup(
    client: httpx.AsyncClient,
    hpo_terms: list,
    seen_urls: set[str],
    start_ref_id: int,
) -> list[Evidence]:
    """HPO JAX API: 按患者 HPO 术语实时查询关联疾病，替代本地 BM25 库。

    每个 HPO term 查一次 /api/hpo/term/{termId}/diseases，
    汇总去重后转为 Evidence（source="hpo_api"）。
    """
    max_terms = int(os.getenv("HPO_MAX_TERMS", "8"))
    max_per_term = int(os.getenv("HPO_MAX_DISEASES_PER_TERM", "10"))
    timeout = float(os.getenv("HPO_TIMEOUT", "15"))
    out: list[Evidence] = []
    ref_id = start_ref_id
    seen_disease_ids: set[str] = set()
    headers = {"Accept": "application/json", "User-Agent": _user_agent()}

    for term in hpo_terms[:max_terms]:
        term_id = getattr(term, "id", None) or term.get("id", "")
        term_name = getattr(term, "name", None) or term.get("name", term_id)
        if not term_id:
            continue
        url = f"{HPO_API_BASE}/term/{term_id}/diseases"
        try:
            r = await client.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [HPO API] {term_id} 查询失败: {e}")
            continue

        associations = data.get("associations") or []
        count = 0
        for assoc in associations:
            disease_id = (assoc.get("diseaseId") or "").strip()
            disease_name = (assoc.get("diseaseName") or "").strip()
            if not disease_id or disease_id in seen_disease_ids:
                continue
            seen_disease_ids.add(disease_id)

            # 构造跳转 URL：OMIM → omim.org，ORPHA → orpha.net
            if disease_id.startswith("OMIM:"):
                omim_num = disease_id.split(":", 1)[1]
                ev_url = f"https://omim.org/entry/{omim_num}"
            elif disease_id.startswith("ORPHA:"):
                orpha_num = disease_id.split(":", 1)[1]
                ev_url = f"https://www.orpha.net/en/disease/detail/{orpha_num}"
            else:
                ev_url = f"https://hpo.jax.org/browse/disease/{disease_id}"

            if ev_url in seen_urls:
                continue
            seen_urls.add(ev_url)

            snippet = f"Associated with {term_name} ({term_id}). Disease: {disease_name} [{disease_id}]"[:250]
            out.append(
                Evidence(
                    ref_id=ref_id,
                    source="hpo_api",
                    title=disease_name or disease_id,
                    url=ev_url,
                    snippet=snippet,
                )
            )
            ref_id += 1
            count += 1
            if count >= max_per_term:
                break

        print(f"  [HPO API] {term_id} ({term_name}): +{count} 条疾病关联")

    return out


async def fetch_clinvar_gnomad_for_input(
    client: httpx.AsyncClient,
    inp: DiagnosisInput,
    seen_urls: set[str],
    start_ref_id: int,
) -> list[Evidence]:
    """基于病例中的基因/变异检索 ClinVar，并对优先记录查询 gnomAD 等位基因频率。"""
    enable_cv = os.getenv("ENABLE_CLINVAR", "true").lower() == "true"
    enable_gn = os.getenv("ENABLE_GNOMAD", "true").lower() == "true"
    if not enable_cv and not enable_gn:
        return []

    esearch_retmax = int(os.getenv("CLINVAR_ESEARCH_RETMAX", "40"))
    clinvar_cap = int(os.getenv("CLINVAR_MAX_RECORDS_PER_GENE", "5"))
    gnomad_cap = int(os.getenv("GNOMAD_MAX_QUERIES_PER_RUN", "4"))
    dataset = os.getenv("GNOMAD_DATASET", "gnomad_r4").strip() or "gnomad_r4"
    if dataset not in GNOMAD_ALLOWED_DATASETS:
        dataset = "gnomad_r4"

    out: list[Evidence] = []
    ref_id = start_ref_id
    gnomad_used = 0

    max_genes = int(os.getenv("CLINVAR_MAX_GENES_PER_GATHER", "5"))
    targets = iter_clinvar_gene_targets(inp)[: max(1, max_genes)]
    print(
        f"  [ClinVar/gnomAD] 本轮检索基因: {len(targets)} 个"
        f"（上限 CLINVAR_MAX_GENES_PER_GATHER={max_genes}）"
    )

    for gene, hint in targets:
        print(f"    → {gene} …")
        pairs: list[tuple[str, dict]] = []
        if enable_cv:
            pairs = await clinvar_gene_summaries(
                client, gene, hint, esearch_retmax, clinvar_cap
            )
        gnomad_done_for_gene = False
        for uid, rec in pairs:
            url = f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{uid}/"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(
                Evidence(
                    ref_id=ref_id,
                    source="clinvar",
                    title=_clinvar_title(rec),
                    url=url,
                    snippet=_clinvar_snippet(rec),
                )
            )
            ref_id += 1

            if (
                enable_gn
                and (not gnomad_done_for_gene)
                and gnomad_used < gnomad_cap
            ):
                vid = clinvar_record_to_gnomad_variant_id(rec)
                if vid:
                    gnomad_done_for_gene = True
                    await asyncio.sleep(float(os.getenv("GNOMAD_REQUEST_GAP_SEC", "0.35")))
                    gv = await gnomad_variant_lookup(client, vid, dataset)
                    gnomad_used += 1
                    if gv:
                        gurl = f"https://gnomad.broadinstitute.org/variant/{vid}?dataset={dataset}"
                        if gurl not in seen_urls:
                            seen_urls.add(gurl)
                            out.append(
                                Evidence(
                                    ref_id=ref_id,
                                    source="gnomad",
                                    title=f"gnomAD {dataset} {gv.get('variant_id', vid)}",
                                    url=gurl,
                                    snippet=gnomad_variant_to_snippet(gv),
                                )
                            )
                            ref_id += 1
    return out
