"""Streamlit UI：罕见病诊断 Agent 交互界面。

布局：
- 左侧 Sidebar：加载 outputs/*.json 结果 / 示例预览 / 选择病例 +「开始诊断」
- 主区域 Tabs：
    Tab 1 诊断报告（临床风格 CSS + 二级标题分节卡片 + 结构化假设）
    Tab 2 引用证据（DataFrame，URL 可点击）
    Tab 3 原始输出（JSON）
- 顶部 st.status() 展示 4 步进度
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# 仅作用于「分节卡片」内的 Markdown，避免污染 Sidebar 标题样式
_REPORT_SECTION_CSS = """
<style>
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] h1 {
        font-size: 1.35rem !important;
        font-weight: 700 !important;
        color: #0d2137 !important;
        border-bottom: 2px solid #1565c0;
        padding-bottom: 0.35rem;
        margin-top: 0.2rem !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] h2 {
        font-size: 1.05rem !important;
        color: #1565c0 !important;
        margin-top: 1rem !important;
        margin-bottom: 0.5rem !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] h3 {
        font-size: 0.98rem !important;
        color: #37474f !important;
        margin-top: 0.85rem !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] hr {
        border: none;
        border-top: 1px solid #e0e0e0;
        margin: 1rem 0;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] ul {
        margin-left: 0.2rem;
        padding-left: 1.1rem;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] li {
        margin-bottom: 0.35rem;
        line-height: 1.55;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] li {
        font-size: 0.95rem;
        color: #263238;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] strong {
        color: #0d2137;
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] blockquote {
        border-left: 4px solid #1565c0;
        padding-left: 0.75rem;
        margin: 0.5rem 0;
        color: #455a64;
        font-size: 0.92rem;
    }
</style>
"""

_CONF_CN = {"high": "高", "medium": "中", "low": "低"}


def _split_level2_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """按 Markdown 二级标题（## 且非 ###）分节，用于分卡片展示。"""
    text = md.strip()
    pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : body_end].strip()
        sections.append((title, body))
    return preamble, sections


def _parse_h1_preamble(preamble: str) -> tuple[str | None, str]:
    """若前言以单个 # 标题开头，拆出主标题与剩余片段。"""
    lines = preamble.strip().split("\n")
    if not lines or not lines[0].startswith("# ") or lines[0].startswith("##"):
        return None, preamble.strip()
    main_title = lines[0].lstrip("#").strip()
    rest = "\n".join(lines[1:]).strip()
    return main_title, rest


def _load_diagnosis_output_from_dict(data: dict, source_label: str | None = None):
    """将完整诊断 JSON（DiagnosisOutput 形态）解析为模型实例，供可视化 Tab 使用。"""
    from schemas import DiagnosisOutput

    try:
        out = DiagnosisOutput.model_validate(data)
    except Exception as e:
        st.error(f"不是有效的诊断结果 JSON（需含 report_markdown、evidences 等）: {e}")
        return None
    meta = dict(out.meta or {})
    if source_label:
        meta["loaded_from_file"] = source_label
    return out.model_copy(update={"meta": meta})


def _load_demo_output():
    """离线加载示例报告，用于无 LLM 时预览 UI。"""
    from schemas import DiagnosisOutput, Evidence

    demo_md = Path("data/samples/demo_report.md")
    if not demo_md.is_file():
        return None
    report = demo_md.read_text(encoding="utf-8")
    evidences = [
        Evidence(
            ref_id=i,
            source="local_kb",
            title=f"示例证据条目 {i}",
            url=f"https://example.invalid/ref/{i}",
            snippet="离线预览用占位摘要。",
        )
        for i in range(1, 11)
    ]
    return DiagnosisOutput(
        patient_id="DEMO-P002",
        hypotheses=[],
        report_markdown=report,
        evidences=evidences,
        meta={
            "demo": True,
            "note": "示例 Markdown，未调用模型；完整诊断请使用「开始诊断」",
        },
    )


def _render_hypothesis_cards(output) -> None:
    """展示模型返回的结构化假设（若有）。"""
    hyps = getattr(output, "hypotheses", None) or []
    if not hyps:
        return
    st.subheader("结构化诊断假设")
    sorted_h = sorted(hyps, key=lambda x: x.rank)
    for row in (sorted_h[i : i + 3] for i in range(0, len(sorted_h), 3)):
        cols = st.columns(len(row))
        for col, h in zip(cols, row):
            with col:
                with st.container(border=True):
                    cn = _CONF_CN.get(h.confidence, h.confidence)
                    st.markdown(f"**Rank {h.rank}** · 置信度 **{cn}**")
                    st.markdown(f"##### {h.disease_name}")
                    if h.disease_id:
                        st.caption(h.disease_id)
                    if h.genetic_support:
                        st.markdown(f"遗传支持: `{h.genetic_support}`")
                    st.markdown(h.one_line_reason)


def _render_report_visual(output) -> None:
    """临床风格报告：顶栏元数据 + 假设卡片 + 分节容器 + 下载。"""
    st.markdown(_REPORT_SECTION_CSS, unsafe_allow_html=True)

    meta = output.meta or {}
    demo = meta.get("demo") is True
    m1, m2, m3, m4 = st.columns([1.2, 1, 1, 1])
    with m1:
        st.markdown("##### 患者 / 会话")
        st.markdown(f"`{output.patient_id}`")
    with m2:
        st.markdown("##### 耗时")
        st.markdown(f"{meta.get('elapsed_seconds', '—')} s")
    with m3:
        st.markdown("##### 证据条数")
        st.markdown(str(len(output.evidences)))
    with m4:
        st.markdown("##### 模式")
        if demo:
            st.markdown("示例预览")
        elif meta.get("loaded_from_file"):
            st.markdown("已保存 JSON")
        else:
            st.markdown("模型报告")

    if demo:
        st.info(meta.get("note", "当前为离线示例报告，仅用于界面预览。"))
    elif meta.get("loaded_from_file"):
        st.success(f"当前展示文件：**{meta['loaded_from_file']}**（未重新调用模型）。")

    _render_hypothesis_cards(output)

    preamble, sections = _split_level2_sections(output.report_markdown)

    if not sections:
        with st.container(border=True):
            st.markdown(output.report_markdown)
    else:
        main_title, preamble_rest = _parse_h1_preamble(preamble)
        if main_title:
            st.markdown(f"# {main_title}")
        if preamble_rest:
            with st.container(border=True):
                st.markdown(preamble_rest)
        for sec_title, sec_body in sections:
            with st.container(border=True):
                st.markdown(f"### {sec_title}")
                st.markdown(sec_body)

    st.download_button(
        label="下载报告（Markdown）",
        data=output.report_markdown.encode("utf-8"),
        file_name=f"diagnosis_{output.patient_id}.md",
        mime="text/markdown",
        key="dl_md_report",
    )


def run_async(coro):
    """在 Streamlit 同步上下文中执行异步协程。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import nest_asyncio  # type: ignore[import]
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)


def render_sidebar() -> dict | None:
    """渲染左侧 sidebar，返回解析后的病例 dict（用户点击"开始诊断"时），或 None。"""
    with st.sidebar:
        st.title("🧬 罕见病诊断 Agent")
        st.divider()

        # ── LLM 配置信息 ──────────────────────────────────────────
        st.subheader("LLM 配置")
        st.info(
            f"**模型**: {os.getenv('LLM_MODEL', 'Qwen/Qwen2.5-14B-Instruct-AWQ')}\n\n"
            f"**端点**: {os.getenv('LLM_BASE_URL', 'http://localhost:8000/v1')}\n\n"
        )
        st.caption("变异数据实时拉取自 HPO API / ClinVar / gnomAD")
        st.divider()

        # ── 病例选择 ──────────────────────────────────────────────
        st.subheader("选择病例")
        samples_dir = Path("data/samples")
        sample_files = sorted(samples_dir.glob("*.json")) if samples_dir.exists() else []

        case_data: dict | None = None

        if sample_files:
            selected = st.selectbox(
                "示例病例",
                options=[None] + sample_files,
                format_func=lambda p: "（请选择）" if p is None else p.name,
            )
            if selected:
                with open(selected, encoding="utf-8") as f:
                    case_data = json.load(f)
                st.success(f"已选: {selected.name}")

        uploaded = st.file_uploader("或上传病例 JSON", type=["json"])
        if uploaded:
            try:
                case_data = json.load(uploaded)
                st.success(f"已上传: {uploaded.name}")
            except Exception as e:
                st.error(f"JSON 解析失败: {e}")

        if case_data:
            st.json(case_data, expanded=False)

        st.divider()
        st.subheader("可视化已跑完的结果")
        out_dir = Path("outputs")
        result_files = sorted(out_dir.glob("*_result.json")) if out_dir.exists() else []
        if result_files:
            pick = st.selectbox(
                "outputs 目录下的结果",
                options=result_files,
                format_func=lambda p: p.name,
                key="saved_result_select",
            )
            if st.button(
                "加载到可视化页面",
                use_container_width=True,
                help="打开下方 Tab「诊断报告」查看分节卡片，无需再跑模型",
            ):
                try:
                    raw = json.loads(pick.read_text(encoding="utf-8"))
                except Exception as e:
                    st.error(f"读取失败: {e}")
                else:
                    loaded = _load_diagnosis_output_from_dict(raw, source_label=pick.name)
                    if loaded is not None:
                        st.session_state["output"] = loaded
                        st.rerun()
        else:
            st.caption("暂无 `outputs/*_result.json`，可先运行 `python main.py ...` 生成。")

        up_out = st.file_uploader("或上传诊断结果 JSON", type=["json"], key="upload_diagnosis_output")
        if up_out is not None and st.button("解析并展示上传的结果", use_container_width=True):
            try:
                raw = json.load(up_out)
            except Exception as e:
                st.error(f"JSON 解析失败: {e}")
            else:
                loaded = _load_diagnosis_output_from_dict(raw, source_label=up_out.name)
                if loaded is not None:
                    st.session_state["output"] = loaded
                    st.rerun()

        st.divider()
        if st.button(
            "预览报告 UI（示例，无需 LLM）",
            use_container_width=True,
            help="加载 data/samples/demo_report.md，立即查看分节卡片与样式",
        ):
            demo_out = _load_demo_output()
            if demo_out is None:
                st.error("未找到 data/samples/demo_report.md")
            else:
                st.session_state["output"] = demo_out
                st.rerun()

        st.divider()
        start = st.button(
            "开始诊断",
            type="primary",
            use_container_width=True,
            disabled=(case_data is None),
        )

        return case_data if start else None


def render_main(case_data: dict | None) -> None:
    """渲染主区域（进度条 + 三个 Tab）。"""
    # ── 已有结果展示 ──────────────────────────────────────────────
    if "output" in st.session_state and not case_data:
        _render_tabs(st.session_state["output"])
        return

    if case_data is None:
        st.info(
            "在左侧 Sidebar：**加载 outputs 里的结果** 或 **上传诊断 JSON**，即可直接看可视化；"
            "或选择病例 JSON 后点击「开始诊断」重新跑一遍。"
        )
        return

    # ── 运行诊断 ──────────────────────────────────────────────────
    from agent import DiagnosisAgent
    from schemas import DiagnosisInput

    try:
        inp = DiagnosisInput.model_validate(case_data)
    except Exception as e:
        st.error(f"病例 JSON 格式错误: {e}")
        return

    progress_msgs: list[str] = []

    with st.status("诊断分析中...", expanded=True) as status:
        def on_progress(msg: str) -> None:
            progress_msgs.append(msg)
            status.update(label=msg)
            st.write(msg)

        try:
            agent = DiagnosisAgent()
            output = run_async(agent.diagnose(inp, progress_callback=on_progress))
            st.session_state["output"] = output
            status.update(label="诊断完成", state="complete")
        except Exception as e:
            status.update(label=f"诊断失败: {e}", state="error")
            st.exception(e)
            return

    _render_tabs(output)


def _render_tabs(output) -> None:
    """渲染三个 Tab 的内容。"""
    tab1, tab2, tab3 = st.tabs(["📋 诊断报告", "📚 引用证据", "🔧 原始输出"])

    # ── Tab 1：诊断报告（可视化布局）────────────────────────────────
    with tab1:
        _render_report_visual(output)

    # ── Tab 2：引用证据 ────────────────────────────────────────────
    with tab2:
        if output.evidences:
            rows = [
                {
                    "引用 [N]": f"[{e.ref_id}]",
                    "来源": e.source,
                    "标题": e.title,
                    "摘要": e.snippet,
                    "URL": e.url,
                }
                for e in output.evidences
            ]
            df = pd.DataFrame(rows)
            # URL 列渲染为可点击链接
            st.dataframe(
                df,
                column_config={
                    "URL": st.column_config.LinkColumn("URL", display_text="链接"),
                },
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("无证据记录。")

    # ── Tab 3：原始输出 ────────────────────────────────────────────
    with tab3:
        st.json(output.model_dump())
        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                label="下载 JSON",
                data=json.dumps(output.model_dump(), ensure_ascii=False, indent=2).encode("utf-8"),
                file_name=f"output_{output.patient_id}.json",
                mime="application/json",
            )
        with col2:
            st.write("**元数据**")
            st.json(output.meta)


def main() -> None:
    st.set_page_config(
        page_title="罕见病诊断 Agent",
        page_icon="🧬",
        layout="wide",
    )
    case_data = render_sidebar()
    render_main(case_data)


if __name__ == "__main__":
    main()
