# 生产环境需要进一步优化（多阶段构建、非 root 用户、健康检查等）
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

# ── 系统依赖 ─────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-dev \
    python3-pip \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 将 python3.10 设为默认 python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 \
    && update-alternatives --install /usr/bin/python python python3.10 1

# ── 安装 vllm（较大，单独一层利用 Docker 缓存）────────────────────
RUN pip install --no-cache-dir vllm

# ── 复制项目代码 ──────────────────────────────────────────────────────
WORKDIR /app
COPY . /app

# ── 安装项目依赖 ──────────────────────────────────────────────────────
RUN pip install --no-cache-dir -e .

# ── 创建运行时目录 ────────────────────────────────────────────────────
RUN mkdir -p /app/data/ontology /app/data/corpus /app/data/samples /app/models /app/outputs

# vllm API 端口 / Streamlit UI 端口
EXPOSE 8000
EXPOSE 8501

# ── 启动脚本 ──────────────────────────────────────────────────────────
# start_vllm.sh 在后台启动 vllm，然后前台运行 Streamlit
# 生产环境建议用 supervisord 或独立 service 管理进程
RUN chmod +x /app/scripts/start_vllm.sh

CMD ["/bin/bash", "-c", "/app/scripts/start_vllm.sh & sleep 30 && streamlit run /app/app.py --server.port 8501 --server.address 0.0.0.0"]
