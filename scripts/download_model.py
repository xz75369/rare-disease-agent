"""从 ModelScope 下载 Qwen 模型到 ./models/ 目录。

用法：
    python scripts/download_model.py
    python scripts/download_model.py --model Qwen/Qwen2.5-7B-Instruct-AWQ

支持的模型（按显存需求排序）：
    Qwen/Qwen2.5-7B-Instruct-AWQ   ~5GB  VRAM
    Qwen/Qwen2.5-14B-Instruct-AWQ  ~10GB VRAM  (默认)
    Qwen/Qwen2.5-32B-Instruct-AWQ  ~20GB VRAM
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 ModelScope 下载 Qwen 模型权重",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL", "Qwen/Qwen2.5-14B-Instruct-AWQ"),
        help="ModelScope 模型 ID（默认读取 .env 中的 LLM_MODEL）",
    )
    parser.add_argument(
        "--cache-dir",
        default="./models",
        help="模型缓存目录（默认 ./models）",
    )
    return parser.parse_args()


def download(model_id: str, cache_dir: str) -> str:
    """调用 modelscope.snapshot_download 下载模型，返回本地路径。

    Args:
        model_id: ModelScope 模型 ID，如 Qwen/Qwen2.5-14B-Instruct-AWQ
        cache_dir: 本地缓存根目录

    Returns:
        下载完成后的模型本地绝对路径
    """
    try:
        from modelscope import snapshot_download  # type: ignore[import]
    except ImportError:
        print("[ERROR] modelscope 未安装，请运行: pip install modelscope>=1.10")
        sys.exit(1)

    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    print(f"[INFO] 开始下载模型: {model_id}")
    print(f"[INFO] 缓存目录: {Path(cache_dir).resolve()}")
    print("[INFO] 首次下载较大（AWQ 模型约 8-20GB），请耐心等待...")

    local_path = snapshot_download(
        model_id=model_id,
        cache_dir=cache_dir,
    )

    print(f"\n[SUCCESS] 模型下载完成")
    print(f"[INFO] 本地路径: {local_path}")
    return local_path


def main() -> None:
    args = parse_args()
    local_path = download(model_id=args.model, cache_dir=args.cache_dir)

    # 提示下一步操作
    print("\n[NEXT] 启动 vllm server:")
    print(f"  bash scripts/start_vllm.sh")
    print(f"\n[NEXT] 或手动启动（指定模型路径）:")
    print(f"  vllm serve {local_path} --port 8000")


if __name__ == "__main__":
    main()
