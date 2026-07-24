"""为任意 ModelClient 包装 generation 跨度记录的客户端适配器。"""

from __future__ import annotations

import importlib
import json
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


class OpenTelemetryModelClient:
    """在当前实时 OTel Context 中记录模型 generation Span。

    ``OpenTelemetryTracePublisher`` 已将 Planner、Executor、Verifier 等阶段附加为
    当前 Context；此包装器只在其中再嵌套一层 generation Span，因此 SQL、HTTP 等
    自动 instrumentation 也会自然继承正确的父 Span。
    """

    def __init__(
        self,
        client: _SupportsGenerate,
        tracer_provider: Any,
        redactor: Redactor | None = None,
    ) -> None:
        try:
            self._context = importlib.import_module("opentelemetry.context")
            self._trace = importlib.import_module("opentelemetry.trace")
        except ImportError as exc:
            raise ImportError(
                "OpenTelemetryModelClient 需要 OpenTelemetry API，请安装 "
                "matterloop-observability[otel]"
            ) from exc
        self._client = client
        self._tracer = tracer_provider.get_tracer("matterloop.observability")
        self._redactor = redactor or Redactor()

    async def generate(self, request: Any) -> Any:
        """在活动阶段下执行模型调用，并把响应、用量与异常写入 Span。"""
        metadata = getattr(request, "metadata", None) or {}
        run_id = metadata.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return await self._client.generate(request)

        agent = metadata.get("agent")
        span_name = (
            f"generation:{agent}" if isinstance(agent, str) and agent.strip() else "generation"
        )
        try:
            span = self._tracer.start_span(span_name)
            token = self._context.attach(self._trace.set_span_in_context(span))
        except Exception:
            logger.exception("实时 generation 跨度创建失败，改为直接透传模型调用")
            return await self._client.generate(request)
        try:
            try:
                self._set_request_attributes(span, request, run_id, metadata)
            except Exception:
                logger.exception("实时 generation 请求属性记录失败")
            response = await self._client.generate(request)
            try:
                self._set_response_attributes(span, response)
            except Exception:
                logger.exception("实时 generation 响应属性记录失败")
            return response
        except Exception as exc:
            try:
                span.set_status(
                    self._trace.Status(self._trace.StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
                )
            except Exception:
                logger.exception("实时 generation 异常状态记录失败")
            raise
        finally:
            try:
                self._context.detach(token)
                span.end()
            except Exception:
                logger.exception("实时 generation 跨度关闭失败")

    async def aclose(self) -> None:
        """把关闭委托给被包装的客户端。"""
        aclose = getattr(self._client, "aclose", None)
        if callable(aclose):
            await aclose()

    def _set_request_attributes(
        self,
        span: Any,
        request: Any,
        run_id: str,
        metadata: dict[str, Any],
    ) -> None:
        attributes: dict[str, Any] = {
            "matterloop.run_id": run_id,
            "matterloop.input": _serialize_messages(request),
            "matterloop.parameters": _serialize_parameters(request),
        }
        for field, attribute in (("step_id", "matterloop.step_id"), ("agent", "matterloop.agent")):
            value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                attributes[attribute] = value
        for key, value in _redact_attributes(self._redactor, attributes).items():
            span.set_attribute(key, _otel_attribute_value(value))

    def _set_response_attributes(self, span: Any, response: Any) -> None:
        attributes = _response_attributes(self._client, response)
        for key, value in _redact_attributes(self._redactor, attributes).items():
            span.set_attribute(key, _otel_attribute_value(value))


def wrap_otel_model_client(
    client: _SupportsGenerate,
    tracer_provider: Any,
) -> OpenTelemetryModelClient:
    """用实时 OTel Context 包装模型客户端。"""
    return OpenTelemetryModelClient(client, tracer_provider)


def _serialize_messages(request: Any) -> list[dict[str, Any]]:
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


def _serialize_parameters(request: Any) -> dict[str, Any]:
    """提取请求的采样参数。"""
    return {
        field: value
        for field in ("temperature", "max_output_tokens")
        if (value := getattr(request, field, None)) is not None
    }


def _response_attributes(client: _SupportsGenerate, response: Any) -> dict[str, Any]:
    """从模型响应提取输出、用量和模型标识。"""
    attributes: dict[str, Any] = {}
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        attributes["matterloop.output"] = output_text
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
                attributes[f"matterloop.usage.{field}"] = value
    response_id = getattr(response, "response_id", None)
    if isinstance(response_id, str) and response_id.strip():
        attributes["matterloop.response_id"] = response_id
    metadata = getattr(response, "metadata", None) or {}
    model = metadata.get("model")
    if not isinstance(model, str) or not model.strip():
        model = next(
            (
                candidate
                for attribute in ("model", "model_name")
                if isinstance((candidate := getattr(client, attribute, None)), str)
                and candidate.strip()
            ),
            None,
        )
    if isinstance(model, str) and model.strip():
        attributes["matterloop.model"] = model
    return attributes


def _redact_attributes(redactor: Redactor, attributes: dict[str, Any]) -> dict[str, Any]:
    """对 OTel 属性做防御性脱敏，失败时不影响模型调用。"""
    try:
        redacted = redactor.redact(attributes)
        return dict(redacted) if isinstance(redacted, dict) else attributes
    except Exception:
        logger.exception("generation 属性脱敏失败")
        return attributes


def _otel_attribute_value(value: Any) -> Any:
    """把复杂属性转成与离线 JSON 导出一致的 OTel 标量。"""
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
