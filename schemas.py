"""Pydantic v2 数据契约，定义系统所有核心数据结构。

输入：DiagnosisInput（HPOTerm + ExomiserHit）
内部：Evidence / Hypothesis
输出：DiagnosisOutput
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, model_validator


# ── 输入层 ────────────────────────────────────────────────────────────────────

class HPOTerm(BaseModel):
    id: str = Field(description="HPO term ID，如 'HP:0001250'")
    name: str = Field(description="HPO term 人类可读名称，如 'Seizure'")


class ExomiserHit(BaseModel):
    """Exomiser 单条结果。兼容两种格式：
    - 标准格式：exomiser_score / phenotype_score / associated_diseases: list[str]
    - P1 格式：combined_score / priority_score / associated_diseases: list[dict]
    """

    rank: int = Field(description="Exomiser 排名（1 = 最高分）")
    gene_symbol: str = Field(description="HGNC 基因符号，如 'SCN1A'")
    omim_ids: list[str] = Field(default=[], description="关联的 OMIM 基因 ID 列表")
    variant_hgvs: str | None = Field(default=None, description="HGVS 变异标注")
    zygosity: str | None = Field(default=None, description="合子性")
    acmg_classification: str | None = Field(default=None, description="ACMG 分类")
    exomiser_score: float | None = Field(default=None, description="综合得分 [0-1]")
    phenotype_score: float | None = Field(default=None, description="表型匹配得分 [0-1]")
    variant_score: float | None = Field(default=None, description="变异致病性得分 [0-1]")
    inheritance_pattern: str | None = Field(default=None, description="遗传模式")
    associated_diseases: list[str] = Field(default=[], description="关联疾病名称列表")

    @model_validator(mode="before")
    @classmethod
    def normalize_exomiser_fields(cls, data: dict) -> dict:
        """将 Exomiser 原始输出字段名统一映射到内部字段名。"""
        # combined_score → exomiser_score
        if "combined_score" in data and "exomiser_score" not in data:
            data["exomiser_score"] = data.pop("combined_score")
        # priority_score → phenotype_score
        if "priority_score" in data and "phenotype_score" not in data:
            data["phenotype_score"] = data.pop("priority_score")
        # associated_diseases: list[dict] → list[str]（取 disease_name）
        diseases = data.get("associated_diseases", [])
        if diseases and isinstance(diseases[0], dict):
            data["associated_diseases"] = [
                d.get("disease_name", str(d)) for d in diseases
            ]
        return data


class CandidateVariant(BaseModel):
    """
    临床已筛选的候选变异（通常来自 Trio-WGS + ACMG 分类后的结果）。
    与 ExomiserHit 不同，此类用于"医生+生信已经筛出明确变异，需 Agent 辅助 VUS 升级"的场景。
    """
    gene: str = Field(description="基因符号，如 H3-3A")
    hgvs_c: str = Field(description="cDNA 变异，如 NM_002107.7:c.4G>A")
    hgvs_p: Optional[str] = Field(default=None, description="蛋白变异，如 p.Ala2Thr")
    inheritance: str = Field(
        description="遗传模式：de novo / maternal / paternal / unknown"
    )
    acmg_class: str = Field(
        description="ACMG 分级：P (Pathogenic) / LP (Likely Pathogenic) / VUS / LB / B"
    )
    acmg_evidence: list[str] = Field(
        default_factory=list,
        description="ACMG 证据列表，如 ['PS2_Moderate', 'PM1', 'PM2_Supporting']"
    )
    zygosity: str = Field(
        default="heterozygous",
        description="合子性：heterozygous / homozygous / compound heterozygous"
    )
    associated_diseases: list[str] = Field(
        default_factory=list,
        description="相关疾病名称列表，如 ['Bryant-Li-Bhoj neurodevelopmental syndrome']"
    )


class DiagnosisInput(BaseModel):
    patient_id: str = Field(description="患者唯一标识符")
    age: float | None = Field(default=None, description="患者年龄（岁）")
    sex: str | None = Field(default=None, description="性别：M / F / unknown")
    family_history: str = Field(default="", description="家族史描述")
    clinical_text: str = Field(default="", description="自由文本临床描述")
    hpo_terms: list[HPOTerm] = Field(description="观察到的 HPO 表型术语列表")
    exomiser_hits: list[ExomiserHit] = Field(
        default=[], description="Exomiser 基因优先级结果（取 top 5）"
    )
    candidate_variants: list[CandidateVariant] = Field(
        default_factory=list,
        description="临床已筛选的候选变异（VUS/LP/P）。与 exomiser_hits 并列，可独立使用或组合使用。"
    )
    prior_diagnosis: Optional[str] = Field(
        default=None,
        description="外院或既往诊断（如果有），如 '癫痫并精神发育迟滞'"
    )
    excluded_conditions: list[str] = Field(
        default_factory=list,
        description="已排除的疾病，如 ['Fragile X syndrome']"
    )


# ── Agent 内部状态 ─────────────────────────────────────────────────────────────

class Evidence(BaseModel):
    ref_id: int = Field(description="报告中引用编号 [N]")
    source: str = Field(
        description="证据来源：local_kb / pubmed / clinvar / gnomad / exomiser 等"
    )
    title: str = Field(description="证据标题或名称")
    url: str = Field(
        description="证据链接（本地伪 URL 如 https://monarchinitiative.org/disease/MONDO:xxx）"
    )
    snippet: str = Field(max_length=250, description="供 LLM 上下文使用的摘要片段（≤250 字）")


class Hypothesis(BaseModel):
    rank: int = Field(description="排名位置（1 = 最可能）")
    disease_name: str = Field(description="疾病名称")
    disease_id: str | None = Field(
        default=None, description="疾病 ID：OMIM:xxxxxx 或 Orpha:xxxx"
    )
    confidence: str = Field(description="置信度：high / medium / low")
    matching_phenotypes: list[str] = Field(
        default=[], description="支持该诊断的 HPO 术语"
    )
    conflicting_phenotypes: list[str] = Field(
        default=[], description="与该诊断不符的 HPO 术语"
    )
    evidence_refs: list[int] = Field(
        default=[], description="引用的证据编号 [N] 列表"
    )
    genetic_support: str | None = Field(
        default=None, description="基因、变异及 ACMG 分类（或 null）"
    )
    one_line_reason: str = Field(description="含 [N] 引用的一句话临床理由")


# ── 输出层 ────────────────────────────────────────────────────────────────────

class DiagnosisOutput(BaseModel):
    patient_id: str = Field(description="患者 ID（来自输入）")
    hypotheses: list[Hypothesis] = Field(description="最终排名后的 5 个诊断假设")
    report_markdown: str = Field(description="完整中文 Markdown 诊断报告")
    evidences: list[Evidence] = Field(description="全部使用的证据列表（含 ref_id）")
    meta: dict = Field(description="元数据：模型名、耗时、步骤计数等")
    causal_hierarchy: Optional[dict] = Field(
        default=None,
        description="因果分层：main_driver / modifiers / secondary_candidates / uncertain_loci"
    )
    genetic_model_hypotheses: list[dict] = Field(
        default_factory=list,
        description="遗传模型假设列表"
    )
