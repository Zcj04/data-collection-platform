# -*- coding: utf-8 -*-
"""AI 分析师 - 统一 LLM 客户端（OpenAI 兼容接口）

默认对接 DeepSeek（deepseek-v4-flash），通过 config.yaml 的 analyst 段 +
.env 的 ANALYST_* 环境变量配置；也可切换 Qwen / 本地 Ollama 等 OpenAI 兼容服务。
"""

import os
from typing import Any, Dict, List, Optional

from openai import OpenAI

from core.config import get as config_get
from core.config import load_env


class AnalystError(Exception):
    """AI 分析师模块统一异常"""


def _resolve(env_key: str, cfg_key: str, default: str) -> str:
    load_env()
    return os.environ.get(env_key) or str(config_get(cfg_key, default) or default)


def get_api_key() -> str:
    return _resolve("ANALYST_API_KEY", "analyst.api_key", "")


def get_base_url() -> str:
    return _resolve(
        "ANALYST_BASE_URL",
        "analyst.base_url",
        "https://api.deepseek.com",
    )


def get_model() -> str:
    return _resolve("ANALYST_MODEL", "analyst.model", "deepseek-v4-flash")


def get_provider() -> str:
    return str(config_get("analyst.provider", "deepseek") or "deepseek")


def get_temperature() -> float:
    try:
        return float(config_get("analyst.temperature", 0.3))
    except (TypeError, ValueError):
        return 0.3


def get_max_tool_rounds() -> int:
    try:
        return int(config_get("analyst.max_tool_rounds", 6))
    except (TypeError, ValueError):
        return 6


def get_timeout() -> float:
    try:
        return float(config_get("analyst.timeout", 90))
    except (TypeError, ValueError):
        return 90.0


def is_configured() -> bool:
    return bool(get_api_key())


def _client() -> OpenAI:
    api_key = get_api_key()
    if not api_key:
        raise AnalystError(
            "未配置模型 API Key：请在项目根目录 .env 中填写 ANALYST_API_KEY"
        )
    return OpenAI(
        api_key=api_key,
        base_url=get_base_url(),
        timeout=get_timeout(),
    )


def _friendly_error(e: Exception) -> str:
    name = type(e).__name__
    msg = str(e)
    if "AuthenticationError" in name or "401" in msg:
        return "模型 API Key 无效或已过期，请检查 .env 中的 ANALYST_API_KEY"
    if "RateLimit" in name or "429" in msg:
        return "模型服务请求过于频繁，请稍后再试"
    if "InsufficientQuota" in name or "402" in msg:
        return "模型 API 账户余额不足，请到服务商平台充值"
    if "Timeout" in name or "Connection" in name or "APIConnectionError" in name:
        return "无法连接模型服务或请求超时，请检查网络与 ANALYST_BASE_URL"
    return f"模型调用失败：{msg[:300]}"


def chat(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    response_format: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
) -> Any:
    """调用模型，返回 response.choices[0].message

    Args:
        messages: OpenAI 消息列表
        tools: 可选 function calling 工具定义
        response_format: 可选 {"type": "json_object"} 等结构化输出要求
        model: 可选模型名覆盖（默认用 analyst.model 配置）
    """
    client = _client()
    kwargs: Dict[str, Any] = {
        "model": model or get_model(),
        "messages": messages,
        "temperature": get_temperature(),
    }
    if tools:
        kwargs["tools"] = tools
    if response_format:
        kwargs["response_format"] = response_format
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:  # noqa: BLE001 - 统一转友好错误
        raise AnalystError(_friendly_error(e)) from e
    return resp.choices[0].message


def chat_stream(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
):
    """流式调用模型：逐块 yield chunk（含 content / tool_calls 增量）"""
    client = _client()
    kwargs: Dict[str, Any] = {
        "model": get_model(),
        "messages": messages,
        "temperature": get_temperature(),
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
    try:
        stream = client.chat.completions.create(**kwargs)
        for chunk in stream:
            yield chunk
    except Exception as e:  # noqa: BLE001 - 统一转友好错误
        raise AnalystError(_friendly_error(e)) from e
