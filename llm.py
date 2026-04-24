"""vllm 本地 LLM 调用封装。

所有 LLM 调用统一通过 llm_json()，使用 openai SDK 连接本地 vllm OpenAI 兼容 API。
患者信息绝不传输至外部：LLM_BASE_URL 必须指向本地服务。

JSON 容错解析策略（针对小模型）：
  1. 直接 json.loads()
  2. 提取 ```json ... ``` 或 ``` ... ``` fence 内容后重试
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """获取（或懒加载）AsyncOpenAI 客户端。

    自动读取系统代理环境变量（HTTPS_PROXY / HTTP_PROXY），
    确保在 Zscaler 等企业代理环境下也能正常连接外部 API。
    """
    global _client
    if _client is None:
        base_url = os.getenv("LLM_BASE_URL", "http://localhost:8000/v1")
        api_key = os.getenv("LLM_API_KEY", "EMPTY")

        # 读取系统代理（Zscaler 等企业代理），并跳过 SSL 验证
        # （Zscaler 做 TLS 解密，Python 默认不信任其自签证书）
        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        http_client = httpx.AsyncClient(
            proxy=proxy_url,
            verify=False,  # Zscaler TLS 拦截场景
        )

        _client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
        )
    return _client


def _extract_json(text: str) -> dict:
    """从 LLM 输出中鲁棒提取 JSON。处理三种情况：
    1. 纯 JSON
    2. ```json ... ``` 或 ``` ... ``` 包裹
    3. 前后有杂文本但中间有 JSON

    Raises:
        ValueError: 三种方式均无法解析时抛出，由调用方决定是否重试。
    """
    text = text.strip()

    # 方式 1：直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 方式 2：剥离 markdown fence
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 方式 3：截取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot extract JSON from LLM output (first 300 chars): {text[:300]}")


async def llm_json(
    system: str,
    user: str,
    temperature: float = 0.2,
    max_retries: int = 2,
) -> dict[str, Any]:
    """调本地 vllm 的 LLM 并返回 JSON dict。对小模型的输出做容错解析。

    Args:
        system: System prompt（≤500 token 为宜）
        user: User prompt（≤1500 token 为宜）
        temperature: 生成温度，报告生成建议用 0.1，诊断用 0.2
        max_retries: JSON 解析失败时重试 LLM 调用的次数（不含首次）

    Returns:
        解析后的字典。解析全部失败时返回 {"error": "...", "raw": "..."} 不抛异常。
    """
    client = get_client()
    model = os.getenv("LLM_MODEL", "Qwen/Qwen2.5-14B-Instruct-AWQ")
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2048"))

    last_error: Exception | None = None
    last_raw: str = ""

    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = 2 ** attempt  # 指数退避：2s, 4s
            print(f"  [llm_json] 重试 {attempt}/{max_retries}，等待 {wait}s...")
            await asyncio.sleep(wait)

        t0 = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            last_raw = raw

            # 简单 token 使用日志
            usage = response.usage
            elapsed = time.perf_counter() - t0
            if usage:
                print(
                    f"  [llm_json] tokens: prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens} "
                    f"elapsed={elapsed:.1f}s"
                )

            return _extract_json(raw)

        except ValueError as e:
            last_error = e
            print(f"  [llm_json] JSON 解析失败（第 {attempt + 1} 次）: {e}")
        except Exception as e:
            last_error = e
            print(f"  [llm_json] LLM 调用失败（第 {attempt + 1} 次）: {e}")

    # 全部重试耗尽，返回错误结构不崩溃
    return {
        "error": str(last_error),
        "raw": last_raw[:500],
    }


async def llm_text(
    system: str,
    user: str,
    temperature: float = 0.1,
) -> str:
    """调本地 vllm 返回原始文本（用于非 JSON 输出场景）。"""
    client = get_client()
    model = os.getenv("LLM_MODEL", "Qwen/Qwen2.5-14B-Instruct-AWQ")
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2048"))

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""
