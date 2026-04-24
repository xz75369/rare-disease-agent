"""4 步主流程 Agent：罕见病诊断核心逻辑。

流程：
  Step 1 — RAG 检索（初始，基于 HPO）
  Step 2 — 初步诊断（LLM → top 5 假设）
  Step 3 — 定向检索 + 自反思（针对 top 2 假设扩充证据，LLM 批判重排）
  Step 4 — 最终报告（LLM → 中文 Markdown + [N] 引用验证）
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from llm import llm_json
from prompts import (
    PROMPT_FINAL,
    PROMPT_INITIAL,
    PROMPT_REFLECT,
    build_evidence_block,
    build_patient_block,
)
from rag import LocalKBRetriever, gather_evidence
from schemas import DiagnosisInput, DiagnosisOutput, Evidence, Hypothesis


class DiagnosisAgent:
    """单 Agent 罕见病诊断系统，通过自反思循环控制幻觉。"""

    def __init__(self, kb_retriever: LocalKBRetriever) -> None:
        self.kb = kb_retriever
        self.evidences: list[Evidence] = []
        self._next_ref_id: int = 1

    def _add_evidences(self, new_evs: list[Evidence]) -> None:
        """将新证据加入 memory，按 URL 去重，重新分配 ref_id。"""
        existing_urls = {e.url for e in self.evidences}
        for ev in new_evs:
            if ev.url not in existing_urls:
                existing_urls.add(ev.url)
                ev.ref_id = self._next_ref_id
                self._next_ref_id += 1
                self.evidences.append(ev)

    def _indexed_evidence_text(self) -> str:
        """返回供 Prompt 使用的格式化证据列表（top 10 条，每条 snippet ≤200 字）。"""
        return build_evidence_block(self.evidences[:10])

    def _parse_hypotheses(self, raw: dict) -> list[Hypothesis]:
        """从 LLM 返回的 dict 中安全解析 Hypothesis 列表。"""
        hyps: list[Hypothesis] = []
        for item in raw.get("hypotheses", [])[:5]:
            try:
                hyps.append(Hypothesis(**item))
            except Exception as e:
                print(f"  [WARN] 假设解析跳过: {e}")
        return hyps

    def _validate_refs(self, markdown: str) -> str:
        """验证报告中的 [N] 引用，将非法引用替换为 [?]。

        合法引用：N 必须在 self.evidences 的 ref_id 中。
        """
        valid_refs = {e.ref_id for e in self.evidences}

        def replace(match: re.Match) -> str:
            n = int(match.group(1))
            return f"[{n}]" if n in valid_refs else "[?]"

        return re.sub(r"\[(\d+)\]", replace, markdown)

    async def diagnose(
        self,
        inp: DiagnosisInput,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> DiagnosisOutput:
        """主流程：4 步诊断，返回 DiagnosisOutput。

        Args:
            inp: 患者病例（HPO + Exomiser）
            progress_callback: 可选进度回调，用于 Streamlit 实时展示
        """
        t_total = time.perf_counter()

        def log(msg: str) -> None:
            print(msg)
            if progress_callback:
                progress_callback(msg)

        # ── Step 1：基于 HPO 的初始检索 ──────────────────────────────────────
        log("[1/4] 初始证据检索（本地 BM25 + 可选 PubMed）...")
        t1 = time.perf_counter()

        # 取 top 5 HPO 名称拼接为 BM25 查询
        hpo_query = " ".join(t.name for t in inp.hpo_terms[:5])
        # 追加 Exomiser top gene 提升相关性
        gene_query = " ".join(h.gene_symbol for h in inp.exomiser_hits[:3])
        initial_query = f"{hpo_query} {gene_query}".strip()

        existing_urls = {e.url for e in self.evidences}
        new_evs = await gather_evidence(
            queries=[initial_query],
            kb=self.kb,
            existing_urls=existing_urls,
            start_ref_id=self._next_ref_id,
        )
        self._add_evidences(new_evs)
        log(f"  → 检索到 {len(new_evs)} 条证据，当前累计 {len(self.evidences)} 条（{time.perf_counter()-t1:.1f}s）")

        # ── Step 2：初步诊断 ──────────────────────────────────────────────────
        log("[2/4] 初步诊断（LLM 生成 top 5 假设）...")
        t2 = time.perf_counter()

        patient_block = build_patient_block(inp)
        evidence_block = self._indexed_evidence_text()
        user_initial = (
            f"{patient_block}\n\n"
            f"Evidence list:\n{evidence_block}\n\n"
            "Propose 5 differential diagnoses. Return ONLY valid JSON matching the schema."
        )

        raw_initial = await llm_json(system=PROMPT_INITIAL, user=user_initial)
        hypotheses = self._parse_hypotheses(raw_initial)

        if not hypotheses:
            log("  [WARN] LLM 未返回有效假设，使用空列表继续")
        else:
            log(f"  → {len(hypotheses)} 个假设（{time.perf_counter()-t2:.1f}s）")
            for h in hypotheses:
                log(f"    #{h.rank} {h.disease_name} [{h.confidence}] {h.one_line_reason[:60]}")

        # ── Step 3：针对 top 2 假设的定向检索 + 自反思 ───────────────────────
        log("[3/4] 定向检索 + 自反思...")
        t3 = time.perf_counter()

        # 对 top 2 假设各做一次定向检索，扩充证据
        top2_queries = [h.disease_name for h in hypotheses[:2]]
        if top2_queries:
            existing_urls = {e.url for e in self.evidences}
            targeted_evs = await gather_evidence(
                queries=top2_queries,
                kb=self.kb,
                existing_urls=existing_urls,
                start_ref_id=self._next_ref_id,
            )
            self._add_evidences(targeted_evs)
            log(f"  → 定向检索新增 {len(targeted_evs)} 条证据，累计 {len(self.evidences)} 条")

        # 自反思：让 LLM 批判每个假设并重排
        evidence_block_updated = self._indexed_evidence_text()
        hyp_json_block = "\n".join(
            f"  Rank {h.rank}: {h.disease_name} ({h.confidence}) — {h.one_line_reason}"
            for h in hypotheses
        )
        user_reflect = (
            f"{patient_block}\n\n"
            f"Initial hypotheses:\n{hyp_json_block}\n\n"
            f"Updated evidence list:\n{evidence_block_updated}\n\n"
            "Review each hypothesis adversarially. Return ONLY valid JSON matching the schema."
        )

        raw_reflect = await llm_json(system=PROMPT_REFLECT, user=user_reflect)

        # 优先使用自反思后的 final_hypotheses；如果解析失败则保留初始假设
        final_hyps_raw = raw_reflect.get("final_hypotheses", [])
        if final_hyps_raw:
            final_hypotheses = []
            for item in final_hyps_raw[:5]:
                try:
                    final_hypotheses.append(Hypothesis(**item))
                except Exception as e:
                    print(f"  [WARN] 自反思假设解析跳过: {e}")
            if not final_hypotheses:
                final_hypotheses = hypotheses
        else:
            final_hypotheses = hypotheses

        log(f"  → 自反思完成（{time.perf_counter()-t3:.1f}s），最终 {len(final_hypotheses)} 个假设")

        # ── Step 4：最终报告 ──────────────────────────────────────────────────
        log("[4/4] 生成最终报告（中文 Markdown + [N] 引用）...")
        t4 = time.perf_counter()

        evidence_block_final = self._indexed_evidence_text()
        final_hyp_block = "\n".join(
            f"  Rank {h.rank}: {h.disease_name} ({h.disease_id or '?'}) "
            f"[{h.confidence}] gene={h.genetic_support or 'none'} "
            f"refs={h.evidence_refs}"
            for h in final_hypotheses
        )
        user_final = (
            f"{patient_block}\n\n"
            f"Final hypotheses:\n{final_hyp_block}\n\n"
            f"Evidence list:\n{evidence_block_final}\n\n"
            "Generate the Chinese Markdown report. Return ONLY valid JSON matching the schema."
        )

        raw_final = await llm_json(
            system=PROMPT_FINAL,
            user=user_final,
            temperature=0.1,  # 报告生成用低温
        )

        report_markdown = raw_final.get("report_markdown", "")
        if not report_markdown:
            # 兜底：LLM 未返回报告时生成最简版
            report_markdown = _fallback_report(inp.patient_id, final_hypotheses, self.evidences)

        # 验证 [N] 引用合法性
        report_markdown = self._validate_refs(report_markdown)
        log(f"  → 报告生成完成（{time.perf_counter()-t4:.1f}s）")

        elapsed = time.perf_counter() - t_total
        log(f"[完成] 总耗时 {elapsed:.1f}s，使用证据 {len(self.evidences)} 条")

        return DiagnosisOutput(
            patient_id=inp.patient_id,
            hypotheses=final_hypotheses,
            report_markdown=report_markdown,
            evidences=self.evidences,
            meta={
                "model": __import__("os").getenv("LLM_MODEL", "unknown"),
                "elapsed_seconds": round(elapsed, 1),
                "evidence_count": len(self.evidences),
                "hypothesis_count": len(final_hypotheses),
                "pubmed_enabled": __import__("os").getenv("ENABLE_PUBMED", "true"),
            },
        )


def _fallback_report(
    patient_id: str,
    hypotheses: list[Hypothesis],
    evidences: list[Evidence],
) -> str:
    """LLM 报告生成失败时的兜底纯文本报告。"""
    lines = [
        f"# 诊断分析报告\n",
        f"**患者 ID**: {patient_id}\n",
        "## 候选诊断\n",
    ]
    for h in hypotheses:
        lines.append(
            f"### Rank {h.rank}: {h.disease_name} ({h.disease_id or '?'})\n"
            f"**置信度**: {h.confidence}  \n"
            f"**遗传支持**: {h.genetic_support or '未提供'}  \n"
            f"**理由**: {h.one_line_reason}\n"
        )
    lines.append("## 参考文献\n")
    for e in evidences[:10]:
        lines.append(f"[{e.ref_id}] ({e.source}) [{e.title}]({e.url})")
    return "\n".join(lines)
