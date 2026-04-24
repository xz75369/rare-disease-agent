# 罕见病诊断辅助系统

基于本地 LLM 的罕见病诊断 Agent，参考 DeepRare (Nature, 2026)。

**患者数据不出站 · 推理可追溯 · 离线优先**

---

## 架构

```
输入 (HPO + Exomiser)
       │
       ▼
┌─────────────────────────────────────────────┐
│              DiagnosisAgent                 │
│                                             │
│  [1] RAG 检索  ←  LocalKBRetriever (BM25)    │
│       + PubMed E-utilities (可选)            │
│                                             │
│  [2] 初步诊断  →  PROMPT_INITIAL             │
│       LLM (vllm · Qwen2.5-14B-AWQ)          │
│                                             │
│  [3] 自反思    →  PROMPT_REFLECT             │
│       定向检索 + adversarial review          │
│                                             │
│  [4] 最终报告  →  PROMPT_FINAL               │
│       中文 Markdown + [N] 引用验证            │
└─────────────────────────────────────────────┘
       │
       ▼
  DiagnosisOutput（报告 + 证据 + 元数据）
```

---

## 快速开始

```bash
# 1. 下载 Qwen 模型（约 8GB）
python scripts/download_model.py

# 2. 下载本体文件
mkdir -p data/ontology
curl -L https://purl.obolibrary.org/obo/hp.obo    -o data/ontology/hp.obo
curl -L https://purl.obolibrary.org/obo/mondo.obo -o data/ontology/mondo.obo

# 3. 准备本地知识库
python scripts/prepare_corpus.py

# 4. 启动 vllm（另开终端）
bash scripts/start_vllm.sh

# 5. 配置环境
cp .env.example .env

# 6. 跑单个病例
python main.py data/samples/mock_case.json

# 7. 或启动 UI
streamlit run app.py
```

### Docker 启动

```bash
cp .env.example .env
docker compose up --build
# UI: http://localhost:8501
# vllm API: http://localhost:8000/v1
```

---

## 核心模块

| 模块 | 职责 |
|------|------|
| `schemas.py` | Pydantic v2 数据契约：`DiagnosisInput` / `DiagnosisOutput` / `Evidence` / `Hypothesis` |
| `prompts.py` | 3 个核心 Prompt（INITIAL / REFLECT / FINAL），针对 Qwen-14B 结构化优化 |
| `llm.py` | `llm_json()` — 调本地 vllm，三层 JSON 容错解析，指数退避重试 |
| `rag.py` | `LocalKBRetriever`（BM25Okapi）+ `pubmed_search()`（可选）+ `gather_evidence()` |
| `agent.py` | `DiagnosisAgent.diagnose()` — 4 步主流程，含 `[N]` 引用合法性验证 |
| `main.py` | CLI 入口 |
| `app.py` | Streamlit UI（3 Tab：报告 / 证据 / 原始 JSON）|

---

## 模型选型

| 模型 | 显存 | 推荐场景 |
|------|------|---------|
| Qwen2.5-7B-Instruct-AWQ | ~5GB | 显存受限 |
| **Qwen2.5-14B-Instruct-AWQ** | **~10GB** | **默认推荐** |
| Qwen2.5-32B-Instruct-AWQ | ~20GB | 现场显存充足时 |

切换：修改 `.env` 中的 `LLM_MODEL`，重新运行 `download_model.py`。

> 不建议使用医疗专用小模型（MedGemma / Baichuan-M1）——通用大模型在罕见病诊断上表现更好（论文 Fig.1c）。

---

## 病例 JSON 格式

```json
{
  "patient_id": "DEMO-001",
  "age": 2.5,
  "sex": "M",
  "family_history": "无类似病史",
  "clinical_text": "6月龄起热性惊厥...",
  "hpo_terms": [
    {"id": "HP:0002373", "name": "Febrile seizure"}
  ],
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

## 已知限制

- 小模型对极罕见病的先验知识有限（发病率 <1:1,000,000 的疾病召回率低）
- 本地知识库覆盖度依赖 MONDO/HPO obo 文件完整性和版本
- PubMed 在线检索需要容器能访问外网（断网时自动降级为纯本地模式）
- 患者数据通过本地模型处理，未实现 PII 脱敏（数据不出容器）
- JSON mode 在极端长上下文时仍可能失败（三层容错解析 + 2 次重试兜底）
