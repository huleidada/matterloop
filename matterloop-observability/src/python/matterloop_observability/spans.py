"""树形 trace 的跨度记录数据模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal

ObservationType = Literal["span", "generation", "evaluator", "tool", "chain", "event"]
"""跨度在 trace 中扮演的观测角色。"""

SpanLevel = Literal["DEFAULT", "ERROR"]
"""跨度的严重级别。"""


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """保存一个已结束跨度的不可变观测记录。

    Args:
        trace_id: 所属 trace 的标识，即产生该跨度的运行 ``run_id``。
        span_id: 跨度的唯一标识。
        parent_span_id: 父跨度标识；根跨度为 ``None``。
        name: 便于人类识别的跨度名称。
        observation_type: 跨度在 trace 中扮演的观测角色。
        started_at: 跨度开始时间。
        ended_at: 跨度结束时间。
        attributes: 跨度附带的输入、输出与元数据；键使用 ``matterloop.`` 前缀风格。
        level: 跨度严重级别，失败路径使用 ``ERROR``。
        status_message: 可选的级别说明，通常记录异常摘要。
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    observation_type: ObservationType | str
    started_at: datetime
    ended_at: datetime
    attributes: Mapping[str, Any] = field(default_factory=dict)
    level: SpanLevel | str = "DEFAULT"
    status_message: str | None = None

    def __post_init__(self) -> None:
        """校验关键标识并冻结属性映射。"""
        if not self.trace_id.strip():
            raise ValueError("span trace_id must not be empty")
        if not self.span_id.strip():
            raise ValueError("span span_id must not be empty")
        if not self.name.strip():
            raise ValueError("span name must not be empty")
        if self.ended_at < self.started_at:
            raise ValueError("span ended_at must not be earlier than started_at")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


def _now() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _OpenSpan:
    """跟踪一个尚未结束、可被查询为父节点的可变跨度。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    observation_type: str
    started_at: datetime
    attributes: dict[str, Any]
    step_id: str | None = None

    def close(
        self,
        ended_at: datetime,
        *,
        level: str = "DEFAULT",
        status_message: str | None = None,
        extra_attributes: Mapping[str, Any] | None = None,
    ) -> SpanRecord:
        """冻结为不可变的跨度记录。"""
        if extra_attributes:
            self.attributes.update(extra_attributes)
        return SpanRecord(
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            name=self.name,
            observation_type=self.observation_type,
            started_at=self.started_at,
            ended_at=max(ended_at, self.started_at),
            attributes=self.attributes,
            level=level,
            status_message=status_message,
        )
