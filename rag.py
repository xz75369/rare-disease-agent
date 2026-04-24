"""RAG 检索层：本地 BM25（HPO/Orphanet/MONDO 知识库）+ 可选 PubMed 在线检索。

设计原则：
- 本地 KB 是主力，即使断网也能运行核心 4 步流程
- PubMed 是补充，超时或不可用时静默跳过
- PubMed 查询只含医学术语（HPO 名称、基因名），绝不含患者个人信息
- 最终返回 ≤10 条 Evidence，每条 snippet ≤200 字
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

from schemas import Evidence

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
        """加载 diseases.jsonl 并构建 BM25 索引（首次调用时执行，此后幂等）。"""
        if self._loaded:
            return

        jsonl_path = self.corpus_dir / "diseases.jsonl"
        if not jsonl_path.exists():
            raise FileNotFoundError(
                f"Corpus not found: {jsonl_path}\n"
                "Run: python scripts/prepare_corpus.py"
            )

        self.documents = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.documents.append(json.loads(line))

        # 每个文档：name + synonyms + definition + HPO IDs 拼接为检索文本
        tokenized_corpus = [self._tokenize(self._doc_text(doc)) for doc in self.documents]
        self.bm25 = BM25Okapi(tokenized_corpus)
        self._loaded = True
        print(f"  [LocalKB] 已加载 {len(self.documents)} 条疾病记录")

    def _doc_text(self, doc: dict) -> str:
        """将文档字段拼接为检索用文本字符串。"""
        parts = [
            doc.get("name", ""),
            " ".join(doc.get("synonyms", [])),
            doc.get("definition", ""),
            " ".join(doc.get("associated_hpo", [])),
            " ".join(doc.get("associated_genes", [])),
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


async def pubmed_search(
    query: str,
    max_results: int = 3,
    timeout: int = 10,
) -> list[dict]:
    """调用 PubMed E-utilities 检索文献，返回元数据列表。

    安全约束：query 只含 HPO 名称、基因名等医学术语，不含患者任何个人信息。
    超时或网络不可用时返回空列表，不抛异常。

    Args:
        query: 医学术语查询字符串（不含患者信息）
        max_results: 最多返回条数
        timeout: HTTP 超时秒数
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # esearch：获取 PMID 列表
            r1 = await client.get(
                f"{base}/esearch.fcgi",
                params={
                    "db": "pubmed",
                    "term": query,
                    "retmax": max_results,
                    "retmode": "json",
                    "sort": "relevance",
                },
            )
            r1.raise_for_status()
            id_list: list[str] = (
                r1.json().get("esearchresult", {}).get("idlist", [])
            )
            if not id_list:
                return []

            # esummary：获取文献元数据
            r2 = await client.get(
                f"{base}/esummary.fcgi",
                params={
                    "db": "pubmed",
                    "id": ",".join(id_list),
                    "retmode": "json",
                },
            )
            r2.raise_for_status()
            summaries = r2.json().get("result", {})

        results = []
        for pmid in id_list:
            if pmid in summaries:
                item = summaries[pmid]
                results.append(
                    {
                        "pmid": pmid,
                        "title": item.get("title", "")[:150],
                        "pubdate": item.get("pubdate", ""),
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    }
                )
        return results

    except Exception as e:
        # 网络不通/超时时静默失败，不影响核心流程
        print(f"  [PubMed] 检索失败（断网或超时，将使用纯本地模式）: {e}")
        return []


async def gather_evidence(
    queries: list[str],
    kb: LocalKBRetriever,
    existing_urls: set[str] | None = None,
    start_ref_id: int = 1,
) -> list[Evidence]:
    """主入口：并行调本地 KB 和 PubMed（如果启用），去重并转为 Evidence。

    Args:
        queries: 检索查询字符串列表（通常 1-2 条）
        kb: 已初始化的 LocalKBRetriever 实例
        existing_urls: 已有证据的 URL 集合，用于去重
        start_ref_id: ref_id 起始编号（续接已有证据）

    Returns:
        新增的 Evidence 列表（不含 existing_urls 中已有的条目）
    """
    existing_urls = existing_urls or set()
    seen_urls: set[str] = set(existing_urls)
    results: list[Evidence] = []
    ref_id = start_ref_id

    enable_pubmed = os.getenv("ENABLE_PUBMED", "true").lower() == "true"
    pubmed_timeout = int(os.getenv("PUBMED_TIMEOUT", "10"))

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
                f"HPO: {' '.join(hpo_list)} "
                f"Genes: {' '.join(gene_list)}"
            )[:200]

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

        # ── PubMed（可选）─────────────────────────────────────────
        if enable_pubmed:
            pubmed_results = await pubmed_search(
                query=query, max_results=3, timeout=pubmed_timeout
            )
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
                        snippet=f"PMID:{item['pmid']} ({item['pubdate']})",
                    )
                )
                ref_id += 1

    return results
