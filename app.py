"""Streamlit UI：罕见病诊断 Agent 交互界面（极简版）。

布局：
- 左侧 Sidebar：选择/上传病例 + LLM 配置信息 + "开始诊断"按钮
- 主区域 Tabs：
    Tab 1 诊断报告（Markdown）
    Tab 2 引用证据（DataFrame，URL 可点击）
    Tab 3 原始输出（JSON）
- 顶部 st.status() 展示 4 步进度
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="罕见病诊断 Agent",
    page_icon="🧬",
    layout="wide",
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


@st.cache_resource(show_spinner="加载本地知识库（BM25）...")
def get_kb():
    """加载 LocalKBRetriever 单例（Streamlit 会话间复用）。"""
    from rag import LocalKBRetriever
    kb = LocalKBRetriever()
    kb.load()
    return kb


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
            f"**PubMed**: {'启用' if os.getenv('ENABLE_PUBMED','true').lower()=='true' else '禁用'}"
        )
        st.caption("患者数据不出站 · 仅使用本地 vllm")
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
        st.info("请在左侧 Sidebar 选择或上传病例 JSON，然后点击「开始诊断」。")
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
            kb = get_kb()
            agent = DiagnosisAgent(kb_retriever=kb)
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

    # ── Tab 1：诊断报告 ────────────────────────────────────────────
    with tab1:
        st.markdown(output.report_markdown)
        st.download_button(
            label="下载报告（Markdown）",
            data=output.report_markdown.encode("utf-8"),
            file_name=f"diagnosis_{output.patient_id}.md",
            mime="text/markdown",
        )

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
    case_data = render_sidebar()
    render_main(case_data)


if __name__ == "__main__":
    main()
