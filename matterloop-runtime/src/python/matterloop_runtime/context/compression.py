"""语义压缩与通用 Tool Result 缩减实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from matterloop_models import (
    MessageRole,
    ModelClient,
    ModelCompactionItem,
    ModelContextScope,
    ModelInputItem,
    ModelMessage,
    ModelMessageItem,
    ModelRequest,
    ModelToolCallItem,
    ModelToolOutputItem,
)


@runtime_checkable
class ContextCompactor(Protocol):
    """把旧上下文压缩为更小的规范输入序列。"""

    async def compact(
        self,
        items: Sequence[ModelInputItem],
        *,
        scope: ModelContextScope,
        target_tokens: int,
    ) -> tuple[ModelInputItem, ...]:
        """压缩输入并保留完成状态、决策和来源。"""
        ...


@runtime_checkable
class ToolResultReducer(Protocol):
    """为已经外置的原始工具结果生成有限模型视图。"""

    def reduce(
        self,
        output: str,
        *,
        artifact_uri: str,
        sha256: str,
        size_bytes: int,
        is_error: bool,
    ) -> str:
        """返回不包含完整原文的有限结果。"""
        ...


class DefaultToolResultReducer:
    """保留首尾片段及完整性元数据的通用缩减器。"""

    def __init__(self, excerpt_characters: int = 1_000) -> None:
        if excerpt_characters < 1:
            raise ValueError("tool result excerpt characters must be positive")
        self._excerpt_characters = excerpt_characters

    def reduce(
        self,
        output: str,
        *,
        artifact_uri: str,
        sha256: str,
        size_bytes: int,
        is_error: bool,
    ) -> str:
        """生成稳定 JSON，避免模型误认为缩减文本是完整结果。"""
        length = self._excerpt_characters
        return json.dumps(
            {
                "is_error": is_error,
                "externalized": True,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "artifact_uri": artifact_uri,
                "head": output[:length],
                "tail": output[-length:] if len(output) > length else "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class SemanticCompactor:
    """使用显式注入且未被生命周期包装的模型生成结构化摘要。"""

    _SCHEMA: Mapping[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "objective": {"type": "string"},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "completed": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "failures": {"type": "array", "items": {"type": "string"}},
            "current_state": {"type": "string"},
            "pending": {"type": "array", "items": {"type": "string"}},
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "facts": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "objective",
            "constraints",
            "completed",
            "decisions",
            "failures",
            "current_state",
            "pending",
            "artifacts",
            "facts",
        ],
    }

    def __init__(
        self,
        model: ModelClient,
        *,
        provider: str,
        model_name: str,
        max_output_tokens: int = 2_048,
    ) -> None:
        if not provider.strip() or not model_name.strip():
            raise ValueError("semantic compactor provider and model must not be empty")
        if max_output_tokens < 1:
            raise ValueError("semantic compactor output limit must be positive")
        self._model = model
        self._provider = provider
        self._model_name = model_name
        self._max_output_tokens = max_output_tokens

    @property
    def provider(self) -> str:
        """返回接收待压缩上下文的供应商标识。"""
        return self._provider

    @property
    def model_name(self) -> str:
        """返回专用摘要模型标识。"""
        return self._model_name

    async def compact(
        self,
        items: Sequence[ModelInputItem],
        *,
        scope: ModelContextScope,
        target_tokens: int,
    ) -> tuple[ModelInputItem, ...]:
        """请求严格 JSON 摘要，并由运行时写入可信来源 ID。"""
        source_ids = tuple(item.item_id for item in items)
        request = ModelRequest(
            messages=(
                ModelMessage(
                    MessageRole.SYSTEM,
                    "压缩 Agent 历史。保留目标、约束、决策、失败、当前状态、待办、"
                    "制品引用和可复用事实。不要添加输入中不存在的信息。",
                ),
                ModelMessage(
                    MessageRole.USER,
                    json.dumps(
                        {
                            "target_tokens": target_tokens,
                            "context_scope": scope.key,
                            "items": [_summary_payload(item) for item in items],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ),
            response_schema=self._SCHEMA,
            response_schema_name="matterloop_context_summary",
            max_output_tokens=self._max_output_tokens,
            metadata={"context_lifecycle_internal": True},
        )
        response = await self._model.generate(request)
        try:
            summary = json.loads(response.output_text)
        except json.JSONDecodeError as exc:
            raise ValueError("semantic compactor returned invalid JSON") from exc
        if not isinstance(summary, dict):
            raise ValueError("semantic compactor summary must be an object")
        summary["source_item_ids"] = list(source_ids)
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return (
            ModelCompactionItem(
                payload=payload,
                provider=self._provider,
                model=self._model_name,
                native=False,
                metadata={"source_item_ids": source_ids},
            ),
        )


def _summary_payload(item: ModelInputItem) -> Mapping[str, object]:
    if item.category is None:
        raise ValueError("model input item category was not normalized")
    base: dict[str, object] = {
        "item_id": item.item_id,
        "category": item.category.value,
    }
    if isinstance(item, ModelMessageItem):
        base.update({"type": "message", "role": item.role.value, "content": item.content})
    elif isinstance(item, ModelToolCallItem):
        base.update(
            {
                "type": "tool_call",
                "call_id": item.call_id,
                "name": item.name,
                "arguments": dict(item.arguments),
            }
        )
    elif isinstance(item, ModelToolOutputItem):
        base.update(
            {
                "type": "tool_output",
                "call_id": item.call_id,
                "output": item.output,
                "is_error": item.is_error,
                "artifact_uri": item.artifact_uri,
            }
        )
    elif isinstance(item, ModelCompactionItem):
        base.update({"type": "compaction", "content": item.payload})
    return base


__all__ = [
    "ContextCompactor",
    "DefaultToolResultReducer",
    "SemanticCompactor",
    "ToolResultReducer",
]
