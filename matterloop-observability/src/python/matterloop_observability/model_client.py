"""为任意 ModelClient 包装 generation 跨度记录的 TracedModelClient。"""

from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import uuid4

from matterloop_observability.pipeline import BatchingPipeline
from matterloop_observability.redaction import Redactor
from matterloop_observability.spans import _now, _OpenSpan
from matterloop_observability.trace_builder import TraceBuilder

logger = logging.getLogger(__name__)


class _SupportsGenerate(Protocol):
    """observability 对模型客户端的最小鸭子类型约定，避免依赖 models 组件。"""

    async def generate(self, request: Any) -> Any:
        """执行一次模型调用并返回响应。"""
        ...


class TracedModelClient:
    """包装模型客户端，把每次调用记录为 trace 中的 generation 跨度。

    ``request.metadata`` 缺少 ``run_id`` 时直接透传调用，保证观测永远不会阻断
    或改变模型调用本身的行为；异常路径记录 ERROR 跨度后原样继续抛出。
    """

    def __init__(
        self,
        client: _SupportsGenerate,
        trace_builder: TraceBuilder | None = None,
        pipeline: BatchingPipeline | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        if trace_builder is None and pipeline is None:
            raise ValueError("trace_builder or pipeline must be provided")
        self._client = client
        self._trace_builder = trace_builder
        self._pipeline = pipeline
        self._redactor = redactor or Redactor()

    async def generate(self, request: Any) -> Any:
        """执行模型调用并记录 generation 跨度。"""
        span = self._begin_span(request)
        try:
            response = await self._client.generate(request)
        except Exception as exc:
            self._finish_span(span, level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise
        self._finish_span(span, request=request, response=response)
        return response

    async def aclose(self) -> None:
        """把关闭委托给被包装的客户端，保持热替换资源清理语义。"""
        aclose = getattr(self._client, "aclose", None)
        if callable(aclose):
            await aclose()

    def _begin_span(self, request: Any) -> _OpenSpan | None:
        """按请求元数据打开 generation 跨度；无法关联 trace 时返回 ``None``。"""
        try:
            metadata = getattr(request, "metadata", None) or {}
            run_id = metadata.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                return None
            step_id = metadata.get("step_id")
            step_id = step_id if isinstance(step_id, str) and step_id.strip() else None
            agent = metadata.get("agent")
            parent_span_id = None
            if self._trace_builder is not None:
                parent_span_id = self._trace_builder.resolve_parent_span_id(run_id, step_id)
            attributes: dict[str, Any] = {
                "matterloop.run_id": run_id,
                "matterloop.input": self._serialize_messages(request),
                "matterloop.parameters": self._serialize_parameters(request),
            }
            if step_id is not None:
                attributes["matterloop.step_id"] = step_id
            if isinstance(agent, str) and agent.strip():
                attributes["matterloop.agent"] = agent
            return _OpenSpan(
                trace_id=run_id,
                span_id=uuid4().hex,
                parent_span_id=parent_span_id,
                name=f"generation:{agent}"
                if isinstance(agent, str) and agent.strip()
                else "generation",
                observation_type="generation",
                started_at=_now(),
                attributes=self._redact(attributes),
                step_id=step_id,
            )
        except Exception:
            logger.exception("generation 跨度创建失败，改为直接透传模型调用")
            return None

    def _finish_span(
        self,
        span: _OpenSpan | None,
        *,
        level: str = "DEFAULT",
        status_message: str | None = None,
        request: Any | None = None,
        response: Any | None = None,
    ) -> None:
        """关闭跨度并送入流水线；任何失败都只记录日志。"""
        if span is None:
            return
        try:
            extra: dict[str, Any] = {}
            if response is not None:
                output_text = getattr(response, "output_text", None)
                if isinstance(output_text, str):
                    extra["matterloop.output"] = output_text
                usage = getattr(response, "usage", None)
                if usage is not None:
                    for field in (
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cache_hit_tokens",
                        "cache_miss_tokens",
                        "reasoning_tokens",
                    ):
                        value = getattr(usage, field, None)
                        if isinstance(value, int):
                            extra[f"matterloop.usage.{field}"] = value
                response_id = getattr(response, "response_id", None)
                if isinstance(response_id, str) and response_id.strip():
                    extra["matterloop.response_id"] = response_id
                model = self._describe_model(response)
                if model is not None:
                    extra["matterloop.model"] = model
            record = span.close(
                _now(),
                level=level,
                status_message=status_message,
                extra_attributes=self._redact(extra),
            )
            pipeline = self._pipeline
            if pipeline is None and self._trace_builder is not None:
                pipeline = self._trace_builder.pipeline
            if pipeline is not None:
                pipeline.enqueue(record)
        except Exception:
            logger.exception("generation 跨度记录失败")

    def _describe_model(self, response: Any) -> str | None:
        """从响应元数据或客户端描述中提取模型名称。"""
        metadata = getattr(response, "metadata", None) or {}
        model = metadata.get("model")
        if isinstance(model, str) and model.strip():
            return model
        for attribute in ("model", "model_name"):
            candidate = getattr(self._client, attribute, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return None

    def _serialize_messages(self, request: Any) -> list[dict[str, Any]]:
        """把请求消息转换为可序列化的结构。"""
        messages = getattr(request, "messages", None) or ()
        serialized: list[dict[str, Any]] = []
        for message in messages:
            role = getattr(message, "role", None)
            role_value = getattr(role, "value", role)
            item: dict[str, Any] = {
                "role": str(role_value),
                "content": getattr(message, "content", ""),
            }
            name = getattr(message, "name", None)
            if isinstance(name, str) and name.strip():
                item["name"] = name
            serialized.append(item)
        return serialized

    def _serialize_parameters(self, request: Any) -> dict[str, Any]:
        """提取请求的采样参数。"""
        parameters: dict[str, Any] = {}
        for field in ("temperature", "max_output_tokens"):
            value = getattr(request, field, None)
            if value is not None:
                parameters[field] = value
        return parameters

    def _redact(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """对跨度属性做防御性脱敏，脱敏失败时保留原值。"""
        try:
            redacted = self._redactor.redact(attributes)
            return dict(redacted) if isinstance(redacted, dict) else attributes
        except Exception:
            logger.exception("generation 属性脱敏失败")
            return attributes


def wrap_model_client(
    client: _SupportsGenerate,
    trace_builder: TraceBuilder,
    pipeline: BatchingPipeline | None = None,
) -> TracedModelClient:
    """用 TraceBuilder 的 registry 与流水线装配一个 TracedModelClient。"""
    return TracedModelClient(client, trace_builder=trace_builder, pipeline=pipeline)
