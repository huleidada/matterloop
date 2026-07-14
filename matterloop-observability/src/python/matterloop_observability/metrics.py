"""内存指标与可选 OpenTelemetry 指标处理器。"""

from __future__ import annotations

import importlib
from collections import Counter

from matterloop_core import LoopEvent


class MetricsHandler:
    """按事件类型累计进程内计数，便于本地观察和测试。"""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def __call__(self, event: LoopEvent) -> None:
        """累计一个生命周期事件。"""
        self._counts[event.event_type.value] += 1

    def count(self, event_name: str) -> int:
        """读取指定事件的累计数量。"""
        return self._counts[event_name]


class OpenTelemetryMetricsHandler:
    """把事件计数写入 OpenTelemetry Meter。"""

    def __init__(self, meter_name: str = "matterloop") -> None:
        try:
            metrics = importlib.import_module("opentelemetry.metrics")
        except ImportError as exc:
            raise RuntimeError(
                "OpenTelemetry 未安装，请安装 matterloop-observability[otel]"
            ) from exc
        meter = metrics.get_meter(meter_name)
        self._counter = meter.create_counter("matterloop.loop.events")

    def __call__(self, event: LoopEvent) -> None:
        """记录一个带事件类型和状态属性的指标。"""
        self._counter.add(
            1,
            {
                "event.type": event.event_type.value,
                "loop.status": event.context.status.value,
            },
        )
