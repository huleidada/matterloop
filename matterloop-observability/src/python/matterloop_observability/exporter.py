"""观测记录的导出协议与 JSONL、OpenTelemetry 两种实现。"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from matterloop_observability.scores import Score
from matterloop_observability.spans import SpanRecord

logger = logging.getLogger(__name__)

ExportItem = SpanRecord | Score
"""导出器接受的观测条目类型。"""


class SpanExporter(Protocol):
    """消费一批已完成跨度和评分的导出协议。"""

    def export(self, batch: Sequence[ExportItem]) -> None:
        """导出一批观测记录；实现抛出的异常会被流水线隔离。"""
        ...


def _jsonable(value: Any) -> Any:
    """把任意观测值转换为可 JSON 序列化的结构。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _span_payload(span: SpanRecord) -> dict[str, Any]:
    """把跨度记录转换为 JSONL 行内容。"""
    return _jsonable(
        {
            "type": "span",
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "observation_type": span.observation_type,
            "started_at": span.started_at,
            "ended_at": span.ended_at,
            "attributes": dict(span.attributes),
            "level": span.level,
            "status_message": span.status_message,
        }
    )


def _score_payload(score: Score) -> dict[str, Any]:
    """把评分记录转换为 JSONL 行内容。"""
    return _jsonable(
        {
            "type": "score",
            "name": score.name,
            "value": score.value,
            "data_type": score.data_type,
            "source": score.source,
            "run_id": score.run_id,
            "step_id": score.step_id,
            "comment": score.comment,
            "evidence": score.evidence,
            "timestamp": score.timestamp,
        }
    )


class JsonlExporter:
    """把观测记录追加写入每行一个 JSON 的本地文件，零额外依赖。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """返回导出文件路径。"""
        return self._path

    def export(self, batch: Sequence[ExportItem]) -> None:
        """把一批记录追加为 JSONL 行。"""
        lines = []
        for item in batch:
            if isinstance(item, SpanRecord):
                payload = _span_payload(item)
            elif isinstance(item, Score):
                payload = _score_payload(item)
            else:
                raise TypeError(f"unsupported export item: {type(item).__name__}")
            lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")


def _otel_attribute_value(value: Any) -> Any:
    """把观测属性值折算为 OTel 支持的标量或标量数组。"""
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _nanoseconds(moment: datetime) -> int:
    """把时间转换为 OTel 使用的 Unix 纳秒时间戳。"""
    return int(moment.timestamp() * 1_000_000_000)


class OtelExporter:
    """把跨度记录以正确的父子关系重建为 OTel 跨度并导出。

    评分无法追加到已结束的跨度上，因此导出为同一 trace 下名为
    ``score:<name>`` 的瞬时子跨度。OTel 由 SDK 分配实际 trace/span ID，原始 MatterLoop
    标识保留在 ``matterloop.trace_id``、``matterloop.span_id`` 和
    ``matterloop.parent_span_id`` 属性中。依赖 ``opentelemetry-sdk`` 与
    ``opentelemetry-exporter-otlp-proto-http``，未安装时构造抛出 ``ImportError``。
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        tracer_provider: Any | None = None,
        service_name: str = "matterloop",
        max_pending_items: int = 10000,
    ) -> None:
        if max_pending_items < 1:
            raise ValueError("max_pending_items must be at least 1")
        self._owns_tracer_provider = tracer_provider is None
        try:
            self._trace = importlib.import_module("opentelemetry.trace")
            sdk_trace = importlib.import_module("opentelemetry.sdk.trace")
            sdk_resources = importlib.import_module("opentelemetry.sdk.resources")
        except ImportError as exc:
            raise ImportError(
                "OtelExporter 需要 OpenTelemetry SDK，请安装 matterloop-observability[otel]"
            ) from exc
        if tracer_provider is None:
            try:
                otlp = importlib.import_module(
                    "opentelemetry.exporter.otlp.proto.http.trace_exporter"
                )
            except ImportError as exc:
                raise ImportError(
                    "OtelExporter 需要 OTLP 导出器，请安装 matterloop-observability[otel]"
                ) from exc
            resource = sdk_resources.Resource.create({"service.name": service_name})
            tracer_provider = sdk_trace.TracerProvider(resource=resource)
            options: dict[str, Any] = {}
            if endpoint is not None:
                options["endpoint"] = endpoint
            if headers is not None:
                options["headers"] = dict(headers)
            tracer_provider.add_span_processor(
                sdk_trace.export.SimpleSpanProcessor(otlp.OTLPSpanExporter(**options))
            )
        self._provider = tracer_provider
        self._tracer = tracer_provider.get_tracer("matterloop.observability")
        self._lock = threading.Lock()
        self._pending: dict[str, list[ExportItem]] = {}
        self._max_pending_items = max_pending_items

    @property
    def tracer_provider(self) -> Any:
        """返回实际写出实时 OTel Span 的 TracerProvider。"""
        return self._provider

    @property
    def owns_tracer_provider(self) -> bool:
        """内部创建 Provider 时为真；其生命周期不能替代应用的全局 Provider。"""
        return self._owns_tracer_provider

    def export(self, batch: Sequence[ExportItem]) -> None:
        """把一批记录按父子关系重建为 OTel 跨度并交给 span processor。"""
        with self._lock:
            pending_trace_ids: set[str] = set()
            for item in batch:
                if isinstance(item, SpanRecord):
                    trace_id = item.trace_id
                elif isinstance(item, Score):
                    trace_id = item.run_id
                else:
                    raise TypeError(f"unsupported export item: {type(item).__name__}")
                pending = self._pending.setdefault(trace_id, [])
                is_root = isinstance(item, SpanRecord) and item.parent_span_id is None
                if len(pending) >= self._max_pending_items and not is_root:
                    logger.warning("OTel trace 根跨度尚未到达，丢弃一条暂存记录")
                    continue
                pending.append(item)
                pending_trace_ids.add(trace_id)
            for trace_id in pending_trace_ids:
                self._export_trace(trace_id)

    def _export_trace(self, trace_id: str) -> None:
        """在根跨度到达后解析一个运行中所有暂存记录的父子关系。"""
        pending = self._pending[trace_id]
        contexts: dict[str, Any] = {}
        root_context: Any | None = None
        while pending:
            next_pending: list[ExportItem] = []
            progressed = False
            for item in pending:
                if isinstance(item, SpanRecord):
                    if item.parent_span_id is None:
                        if root_context is not None:
                            next_pending.append(item)
                            continue
                        context = self._export_span(item, parent_context=None)
                        root_context = context
                        contexts[item.span_id] = context
                        progressed = True
                    elif item.parent_span_id in contexts:
                        contexts[item.span_id] = self._export_span(
                            item,
                            parent_context=contexts[item.parent_span_id],
                        )
                        progressed = True
                    else:
                        next_pending.append(item)
                elif root_context is not None:
                    self._export_score(item, parent_context=root_context)
                    progressed = True
                else:
                    next_pending.append(item)
            pending = next_pending
            if not progressed:
                self._pending[trace_id] = pending
                return
        self._pending.pop(trace_id, None)

    def _start_span(
        self,
        name: str,
        started_at: datetime,
        *,
        parent_context: Any | None,
    ) -> Any:
        """使用公开 OTel API 按父子关系和开始时间创建跨度。"""
        context = None
        if parent_context is not None:
            context = self._trace.set_span_in_context(self._trace.NonRecordingSpan(parent_context))
        return self._tracer.start_span(
            name,
            context=context,
            start_time=_nanoseconds(started_at),
        )

    def _export_span(self, record: SpanRecord, *, parent_context: Any | None) -> Any:
        """把一条跨度记录重建为带有起止时间的 OTel 跨度。"""
        span = self._start_span(
            record.name,
            record.started_at,
            parent_context=parent_context,
        )
        span.set_attribute("matterloop.trace_id", record.trace_id)
        span.set_attribute("matterloop.span_id", record.span_id)
        if record.parent_span_id is not None:
            span.set_attribute("matterloop.parent_span_id", record.parent_span_id)
        for key, value in record.attributes.items():
            span.set_attribute(key, _otel_attribute_value(value))
        if record.level == "ERROR":
            span.set_status(self._trace.Status(self._trace.StatusCode.ERROR, record.status_message))
        span.end(end_time=_nanoseconds(record.ended_at))
        return span.get_span_context()

    def _export_score(self, score: Score, *, parent_context: Any) -> None:
        """把评分导出为同一 trace 下的瞬时跨度。"""
        attributes: dict[str, Any] = {
            "matterloop.trace_id": score.run_id,
            "score.name": score.name,
            "score.value": score.value,
            "score.data_type": score.data_type,
            "score.source": score.source,
        }
        if score.step_id is not None:
            attributes["matterloop.step_id"] = score.step_id
        if score.comment is not None:
            attributes["score.comment"] = score.comment
        if score.evidence:
            attributes["score.evidence"] = [str(item) for item in score.evidence]
        span = self._start_span(
            f"score:{score.name}",
            score.timestamp,
            parent_context=parent_context,
        )
        for key, value in attributes.items():
            span.set_attribute(key, _otel_attribute_value(value))
        span.end(end_time=_nanoseconds(score.timestamp))
