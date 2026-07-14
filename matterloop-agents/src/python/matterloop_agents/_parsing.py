"""集中处理模型 JSON 输出，避免各 Agent 复制宽松解析逻辑。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from matterloop_agents.errors import AgentModelOutputError


def parse_json_object(text: str, *, purpose: str) -> Mapping[str, object]:
    """把模型文本解析成字符串键 JSON 对象。

    Args:
        text: 模型返回的结构化文本。
        purpose: 错误消息中用于定位调用阶段的名称。

    Returns:
        已验证键类型的对象。

    Raises:
        AgentModelOutputError: 文本不是合法 JSON 对象。
    """
    if not text.strip():
        raise AgentModelOutputError(f"{purpose} model output is empty")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentModelOutputError(f"{purpose} model output is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AgentModelOutputError(f"{purpose} model output must be a JSON object")
    return cast(dict[str, object], value)


def require_string(value: Mapping[str, object], key: str, *, purpose: str) -> str:
    """读取非空字符串字段。"""
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise AgentModelOutputError(f"{purpose}.{key} must be a non-empty string")
    return item


def string_tuple(
    value: Mapping[str, object],
    key: str,
    *,
    purpose: str,
    required: bool = True,
) -> tuple[str, ...]:
    """读取只包含非空字符串的数组字段。"""
    item = value.get(key)
    if item is None and not required:
        return ()
    if not isinstance(item, list) or not all(
        isinstance(entry, str) and bool(entry.strip()) for entry in item
    ):
        raise AgentModelOutputError(f"{purpose}.{key} must be an array of non-empty strings")
    return tuple(cast(list[str], item))


def require_boolean(value: Mapping[str, object], key: str, *, purpose: str) -> bool:
    """读取布尔字段，禁止把数字隐式当成布尔值。"""
    item = value.get(key)
    if not isinstance(item, bool):
        raise AgentModelOutputError(f"{purpose}.{key} must be a boolean")
    return item


def require_score(value: Mapping[str, object], key: str, *, purpose: str) -> float:
    """读取零到一百之间的数值评分。"""
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise AgentModelOutputError(f"{purpose}.{key} must be a number")
    score = float(item)
    if not 0 <= score <= 100:
        raise AgentModelOutputError(f"{purpose}.{key} must be between 0 and 100")
    return score
