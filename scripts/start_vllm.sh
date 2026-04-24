#!/bin/bash
# 启动 vllm server，暴露 OpenAI 兼容端口（http://localhost:8000/v1）
# 参数从 .env 读取（已由 Docker ENV 或 dotenv 注入）
#
# 小模型默认参数：
#   --max-model-len 8192          : 上下文长度（14B AWQ 推荐值）
#   --gpu-memory-utilization 0.85 : 留 15% 显存给系统
#   --dtype auto                  : 自动检测 AWQ 量化类型

set -euo pipefail

# ── 读取配置（优先环境变量，其次 .env 文件）────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${PROJECT_DIR}/.env" ]; then
    # 导出 .env 中的变量（跳过注释行和空行）
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_DIR}/.env"
    set +a
fi

MODEL="${LLM_MODEL:-Qwen/Qwen2.5-14B-Instruct-AWQ}"
MODEL_DIR="${PROJECT_DIR}/models"
PORT=8000

MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
GPU_MEMORY_UTIL="${VLLM_GPU_MEMORY_UTIL:-0.85}"

echo "[INFO] vllm 启动配置："
echo "  模型:              ${MODEL}"
echo "  模型目录:          ${MODEL_DIR}"
echo "  端口:              ${PORT}"
echo "  max-model-len:     ${MAX_MODEL_LEN}"
echo "  gpu-memory-util:   ${GPU_MEMORY_UTIL}"
echo ""

# ── 检查模型是否已下载 ───────────────────────────────────────────────
# vllm 接受 ModelScope/HuggingFace ID 或本地路径
# 如果 models/ 目录下已有对应模型，使用本地路径（更快）
MODEL_LOCAL_PATH=$(find "${MODEL_DIR}" -maxdepth 3 -name "config.json" 2>/dev/null \
    | xargs -I{} dirname {} \
    | head -1)

if [ -n "${MODEL_LOCAL_PATH}" ]; then
    echo "[INFO] 使用本地模型: ${MODEL_LOCAL_PATH}"
    SERVE_TARGET="${MODEL_LOCAL_PATH}"
else
    echo "[WARN] 未找到本地模型，将从 ModelScope/HuggingFace 在线加载: ${MODEL}"
    echo "[WARN] 首次运行建议先执行: python scripts/download_model.py"
    SERVE_TARGET="${MODEL}"
fi

# ── 启动 vllm ───────────────────────────────────────────────────────
exec python -m vllm.entrypoints.openai.api_server \
    --model "${SERVE_TARGET}" \
    --port "${PORT}" \
    --host "0.0.0.0" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTIL}" \
    --dtype auto \
    --trust-remote-code \
    --served-model-name "${MODEL}"
