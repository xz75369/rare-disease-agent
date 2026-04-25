# 罕见病诊断辅助系统

基于 DeepSeek API + 四路权威数据库实时检索的罕见病诊断 Agent，参考 DeepRare (Nature, 2026)。

---

## 架构

```
输入 (HPO + 候选变异 / Exomiser)
       │
       ▼
┌──────────────────────────────────────────────────────┐
│                  DiagnosisAgent                      │
│                                                      │
│  [1] RAG 检索                                        │
│      ├── HPO JAX API    → 疾病-表型关联（主力）       │
│      ├── PubMed         → 文献检索（NCBI E-utils）   │
│      ├── ClinVar        → 变异致病性（NCBI E-utils） │
│      └── gnomAD         → 等位基因频率（GraphQL）    │
│                                                      │
│  [2] 初步诊断  →  PROMPT_INITIAL                     │
│      场景 A/B/C 识别 + 变异证据评级 + 因果分层        │
│                                                      │
│  [3] 自反思    →  PROMPT_REFLECT                     │
│      定向检索 + adversarial review + 重排假设         │
│                                                      │
│  [4] 最终报告  →  PROMPT_FINAL                       │
│      中文 Markdown + [N] 引用验证                     │
└──────────────────────────────────────────────────────┘
       │
       ▼
  DiagnosisOutput（报告 + 证据 + 元数据）
```

---

## 数据来源

| 来源 | 接口 | 提供内容 | 环境变量开关 |
|------|------|---------|------------|
| **HPO JAX API** | `hpo.jax.org/api/hpo/term/{id}/diseases` | 疾病-表型关联（替代本地 KB） | `ENABLE_HPO_API=true` |
| **PubMed** | NCBI E-utilities esearch + esummary | 相关文献 | `ENABLE_PUBMED=true` |
| **ClinVar** | NCBI E-utilities（基因名检索） | 变异致病性分级、review status | `ENABLE_CLINVAR=true` |
| **gnomAD** | GraphQL `gnomad.broadinstitute.org/api` | AC/AN/AF/Hom（PM2 证据） | `ENABLE_GNOMAD=true` |

---

## 快速开始

```bash
# 1. 安装依赖
pip install -e .

# 2. 配置环境
cp .env.example .env
# 编辑 .env，填写：
#   LLM_API_KEY=sk-xxx        （DeepSeek API Key）
#   NCBI_EMAIL=you@email.com  （NCBI 推荐填写，提升限额）

# 3. 运行单个病例
python main.py data/samples/P002.json

# 4. 或启动 UI
streamlit run app.py
```

---

## 核心模块

| 模块 | 职责 |
|------|------|
| `schemas.py` | Pydantic v2 数据契约：`DiagnosisInput` / `DiagnosisOutput` / `Evidence` / `Hypothesis` / `CandidateVariant` |
| `prompts.py` | 3 个核心 Prompt（INITIAL / REFLECT / FINAL），含变异证据评级 + 因果分层 + 遗传模型假设 |
| `llm.py` | `llm_json()` — 调 DeepSeek API，三层 JSON 容错解析，指数退避重试 |
| `rag.py` | `gather_evidence()` — 四路 API 检索入口；`build_search_queries()` — PubMed query 构造 |
| `external_db.py` | HPO JAX API / PubMed / ClinVar / gnomAD 具体实现 |
| `agent.py` | `DiagnosisAgent.diagnose()` — 4 步主流程，含 `[N]` 引用合法性验证 |
| `main.py` | CLI 入口 |
| `app.py` | Streamlit UI（3 Tab：报告 / 证据 / 原始 JSON）|

---

## 病例 JSON 格式

### 场景 A：临床候选变异（Trio-WGS 后）

```json
{
  "patient_id": "P002",
  "age": 12,
  "sex": "M",
  "hpo_terms": [
    {"id": "HP:0001250", "name": "Seizure"},
    {"id": "HP:0001263", "name": "Global developmental delay"}
  ],
  "candidate_variants": [
    {
      "gene": "H3-3A",
      "hgvs_c": "NM_002107.7:c.4G>A",
      "hgvs_p": "p.Ala2Thr",
      "inheritance": "de novo",
      "acmg_class": "VUS",
      "acmg_evidence": ["PS2_Moderate", "PM1", "PM2_Supporting"],
      "zygosity": "heterozygous",
      "associated_diseases": ["Bryant-Li-Bhoj neurodevelopmental syndrome 2"]
    }
  ],
  "prior_diagnosis": "癫痫并精神发育迟滞"
}
```

### 场景 B：Exomiser 结果

```json
{
  "patient_id": "DEMO-001",
  "hpo_terms": [{"id": "HP:0002373", "name": "Febrile seizure"}],
  "exomiser_hits": [
    {
      "rank": 1,
      "gene_symbol": "SCN1A",
      "variant_hgvs": "c.4933C>T (p.Arg1645Ter)",
      "acmg_classification": "Pathogenic",
      "exomiser_score": 0.98
    }
  ]
}
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_BASE_URL` | `https://api.deepseek.com` | LLM API 端点 |
| `LLM_API_KEY` | — | API Key（必填） |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `LLM_MAX_TOKENS` | `4096` | 最大输出 token |
| `NCBI_EMAIL` | `anonymous@example.com` | NCBI 推荐填写 |
| `NCBI_API_KEY` | — | NCBI API Key（提升速率上限） |
| `ENABLE_HPO_API` | `true` | 是否启用 HPO JAX API |
| `ENABLE_PUBMED` | `true` | 是否启用 PubMed |
| `ENABLE_CLINVAR` | `true` | 是否启用 ClinVar |
| `ENABLE_GNOMAD` | `true` | 是否启用 gnomAD |
| `HPO_MAX_TERMS` | `8` | HPO API 查询术语上限 |
| `HPO_MAX_DISEASES_PER_TERM` | `10` | 每个 HPO 术语返回疾病上限 |
| `GNOMAD_DATASET` | `gnomad_r4` | gnomAD 数据集版本 |

---

## ACMG 规则

Agent 严格遵守以下规则，无论 LLM 如何回复：

- **ACMG 分级不可更改**：输入是 VUS 则输出仍是 VUS，禁止升级
- **VUS 只说**"意义未明，需进一步验证"
- **LP 只说**"可能致病，支持诊断但非确诊"
- **所有 [N] 引用**必须对应实际检索到的 Evidence，非法引用自动替换为 `[?]`

---

## 已知限制

- 网络不可用时 HPO/ClinVar/gnomAD 自动跳过，LLM 仅凭自身知识推理
- gnomAD 查询依赖 ClinVar 返回坐标（SPDI/GRCh38），部分变异可能查不到
- PubMed snippet 仅含标题和发表日期，不含摘要全文
- JSON mode 在极端长上下文时仍可能失败（三层容错解析 + 2 次重试兜底）
