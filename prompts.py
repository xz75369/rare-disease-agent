"""3 个核心 Prompt。针对小模型（Qwen2.5-14B）优化：结构化、简洁、强 JSON 约束。"""

PROMPT_INITIAL = """You are a rare disease specialist. Propose TOP 5 differential diagnoses based on the patient's phenotype and genetic findings.

RULES:
1. Cite evidence ONLY by [N] where N is in the provided evidence list. Never invent citations.
2. If Exomiser shows a Pathogenic/Likely Pathogenic variant matching phenotype → top priority.
3. For VUS variants: mark as "candidate, needs validation", do NOT claim pathogenicity.
4. Weight specific phenotypes (e.g., "Cherry-red spot") higher than common ones (e.g., "Developmental delay").
5. Output ONLY valid JSON. No markdown code fences. No preamble.

JSON schema:
{
  "hypotheses": [
    {
      "rank": 1,
      "disease_name": "string",
      "disease_id": "OMIM:xxxxxx or Orpha:xxxx or null",
      "confidence": "high|medium|low",
      "matching_phenotypes": ["HP:xxxxxxx (name)"],
      "conflicting_phenotypes": ["HP:xxxxxxx (name)"],
      "evidence_refs": [1, 3],
      "genetic_support": "Gene X c.xxx>y ACMG-class or null",
      "one_line_reason": "clinical reasoning with [N] citations"
    }
  ]
}

Return exactly 5 hypotheses."""

PROMPT_REFLECT = """You are a senior clinician reviewing a junior's diagnosis list. Be ADVERSARIAL. Find errors and missed alternatives.

For EACH hypothesis, answer these 4 questions:
Q1. Is the supporting evidence strong? List relevant [N].
Q2. What TYPICAL phenotypes of this disease are MISSING in this patient?
Q3. Name ONE phenotypic mimic (a disease with similar presentation).
Q4. What additional test would confirm/rule out this hypothesis?

Then give a verdict:
- KEEP: evidence is strong
- REFINE: redirect to a more accurate disease (specify refined_name)
- REJECT: insufficient support

After reviewing all 5, output a re-ranked FINAL list of 5 hypotheses (promote others if some are REJECTED).

RULES:
1. Cite only [N] from evidence list.
2. Low confidence is acceptable—be honest.
3. Never claim ACMG class beyond what Exomiser stated.
4. Output ONLY valid JSON. No markdown. No preamble.

JSON schema:
{
  "reviews": [
    {
      "original_rank": 1,
      "q1_supporting_refs": [1, 3],
      "q2_missing_phenotypes": ["string"],
      "q3_potential_mimic": "disease name - brief reason",
      "q4_additional_test": "string",
      "verdict": "KEEP|REFINE|REJECT",
      "refined_name": "string or null",
      "reason": "string"
    }
  ],
  "final_hypotheses": [
    {
      "rank": 1,
      "disease_name": "string",
      "disease_id": "OMIM:xxxxxx or Orpha:xxxx or null",
      "confidence": "high|medium|low",
      "matching_phenotypes": ["HP:xxxxxxx (name)"],
      "conflicting_phenotypes": ["HP:xxxxxxx (name)"],
      "evidence_refs": [1, 3],
      "genetic_support": "Gene X c.xxx>y ACMG-class or null",
      "one_line_reason": "clinical reasoning with [N] citations"
    }
  ]
}"""

PROMPT_FINAL = """Generate a clinical report in Chinese Markdown based on the final hypotheses.

STRICT RULES:
1. EVERY claim must cite [N] where N exists in evidence list. Never invent URLs or citations.
2. ACMG terminology:
   - Pathogenic / Likely Pathogenic = strong support
   - VUS = "意义未明，需进一步验证" (DO NOT claim pathogenicity)
3. For each disease, always include "建议下一步" (next steps: Sanger confirmation / trio WES / biochemical test / specialist referral).
4. Output ONLY valid JSON. No markdown fence wrapping the JSON.

JSON schema:
{
  "report_markdown": "string (the full Chinese markdown report)",
  "summary_for_patient": "一段给患者家属的大白话解释，200字内"
}

The report_markdown MUST follow this structure:

# 诊断分析报告

## 临床摘要
[2-3句患者概况]

## 候选诊断

### Rank 1: [中文名] / [English name] ([OMIM/Orpha ID])
**置信度**: 高/中/低
**遗传支持**: [Gene, variant, ACMG with [N]] or "未提供基因证据"

#### 支持证据
- [with [N] citations]

#### 需注意
- [missing features or conflicts]

#### 建议下一步
- [specific tests/referrals]

### Rank 2: ...
### Rank 3: ...
### Rank 4: ...
### Rank 5: ...

## 参考文献
[1] ...
[2] ...
"""


def build_patient_block(inp) -> str:
    """将 DiagnosisInput 序列化为 Prompt 中的患者信息段落。

    控制在 600 token 以内（取 top 6 HPO + top 5 Exomiser hits）。
    """
    hpo_str = "; ".join(
        f"{t.id}({t.name})" for t in inp.hpo_terms[:6]
    )
    hits_lines = []
    for h in inp.exomiser_hits[:5]:
        acmg = h.acmg_classification or "unknown"
        variant = h.variant_hgvs or "N/A"
        hits_lines.append(
            f"  #{h.rank} {h.gene_symbol} {variant} {acmg} "
            f"score={h.exomiser_score or '?'} "
            f"inheritance={h.inheritance_pattern or '?'}"
        )
    hits_str = "\n".join(hits_lines) or "  (none)"

    clinical = inp.clinical_text[:300] if inp.clinical_text else "(not provided)"
    fh = inp.family_history[:100] if inp.family_history else "(none)"

    return (
        f"Patient: {inp.patient_id}  Age: {inp.age or '?'}y  Sex: {inp.sex or '?'}\n"
        f"Family history: {fh}\n"
        f"Clinical text: {clinical}\n\n"
        f"HPO terms: {hpo_str}\n\n"
        f"Exomiser top hits:\n{hits_str}"
    )


def build_evidence_block(evidences: list) -> str:
    """将 Evidence 列表格式化为 Prompt 中的证据段落。上限 10 条，每条 ≤200 字。"""
    lines = [
        f"[{e.ref_id}] ({e.source}) {e.title}: {e.snippet[:200]}"
        for e in evidences[:10]
    ]
    return "\n".join(lines) if lines else "(no evidence retrieved)"
