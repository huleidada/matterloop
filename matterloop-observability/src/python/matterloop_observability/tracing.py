"""可选 OpenTelemetry 生命周期追踪处理器。

.. deprecated::
    请改用 :class:`matterloop_observability.TraceBuilder` 搭配
    :class:`matterloop_observability.OtelExporter`，它能基于事件流重建完整的
    树形跨度；本处理器仅为兼容保留，会在后续版本移除。
"""

from __future__ import annotations

import importlib
import warnings

from matterloop_core import LoopEvent


class TracingHandler:
    """为每个生命周期事件创建短跨度 OpenTelemetry Span。

    已废弃：孤立短跨度无法还原父子关系，请改用 ``TraceBuilder``。
    """

    def __init__(self, tracer_name: str = "matterloop") -> None:
        warnings.warn(
            "TracingHandler 已废弃，请改用 TraceBuilder + OtelExporter",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            trace = importlib.import_module("opentelemetry.trace")
        except ImportError as exc:
            raise RuntimeError(
                "OpenTelemetry 未安装，请安装 matterloop-observability[otel]"
            ) from exc
        self._tracer = trace.get_tracer(tracer_name)

    def __call__(self, event: LoopEvent) -> None:
        """记录一个包含运行标识和状态的 Span。"""
        with self._tracer.start_as_current_span(event.event_type.value) as span:
            span.set_attribute("loop.run_id", event.context.run_id)
            span.set_attribute("loop.status", event.context.status.value)
