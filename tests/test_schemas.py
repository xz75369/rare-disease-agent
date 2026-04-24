"""验证 schema 兼容两种输入场景。"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import DiagnosisInput, HPOTerm, CandidateVariant


def test_exomiser_scene():
    """场景 1：Exomiser 输入（向后兼容）"""
    data = {
        "patient_id": "P001",
        "hpo_terms": [{"id": "HP:0001250", "name": "Seizure"}],
        "exomiser_hits": []
    }
    d = DiagnosisInput.model_validate(data)
    assert d.patient_id == "P001"
    assert len(d.candidate_variants) == 0
    print("✅ Exomiser 场景 OK")


def test_clinical_variant_scene():
    """场景 2：临床变异输入（新增场景）"""
    data = {
        "patient_id": "P002",
        "age": 12,
        "sex": "M",
        "hpo_terms": [{"id": "HP:0001250", "name": "Seizure"}],
        "candidate_variants": [
            {
                "gene": "H3-3A",
                "hgvs_c": "NM_002107.7:c.4G>A",
                "hgvs_p": "p.Ala2Thr",
                "inheritance": "de novo",
                "acmg_class": "VUS",
                "acmg_evidence": ["PS2_Moderate", "PM1", "PM2_Supporting"]
            }
        ],
        "prior_diagnosis": "癫痫并精神发育迟滞",
        "excluded_conditions": ["Fragile X syndrome"]
    }
    d = DiagnosisInput.model_validate(data)
    assert d.patient_id == "P002"
    assert len(d.candidate_variants) == 1
    assert d.candidate_variants[0].acmg_class == "VUS"
    print("✅ 临床变异场景 OK")


if __name__ == "__main__":
    test_exomiser_scene()
    test_clinical_variant_scene()
    print("\n所有 schema 测试通过")
