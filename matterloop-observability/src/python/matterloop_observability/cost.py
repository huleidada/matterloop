"""运行与租户级别的进程内成本追踪。"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from matterloop_core import LoopEvent

_USAGE_KEYS = ("tokens_input", "tokens_output", "cost_micro_units", "tool_calls")


@dataclass(frozen=True, slots=True)
class CostRecord:
    """一次用量上报的不可变记录。

    Args:
        run_id: 产生消耗的运行标识。
        tenant_id: 可选的租户标识，用于多租户聚合。
        tokens_input: 输入 token 数。
        tokens_output: 输出 token 数。
        cost_micro_units: 以货币最小计量单位的百万分之一计的成本。
        tool_calls: 工具调用次数。
        recorded_at: 记录产生时间。
    """

    run_id: str
    tenant_id: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    cost_micro_units: int = 0
    tool_calls: int = 0
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """拒绝无法归属运行或用量为负的记录。"""
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        usage = (self.tokens_input, self.tokens_output, self.cost_micro_units, self.tool_calls)
        if any(value < 0 for value in usage):
            raise ValueError("usage values must not be negative")


@dataclass(frozen=True, slots=True)
class CostSummary:
    """一组成本记录的聚合结果。

    Args:
        records: 参与聚合的记录数量。
        tokens_input: 输入 token 总数。
        tokens_output: 输出 token 总数。
        cost_micro_units: 成本总额（百万分之一计量单位）。
        tool_calls: 工具调用总次数。
    """

    records: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_micro_units: int = 0
    tool_calls: int = 0


class CostTracker:
    """线程安全的进程内成本累计器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[CostRecord] = []

    def record(self, record: CostRecord) -> None:
        """追加一条成本记录。

        Args:
            record: 待累计的用量记录。
        """
        with self._lock:
            self._records.append(record)

    def total_for_run(self, run_id: str) -> CostSummary:
        """聚合指定运行的全部成本。"""
        with self._lock:
            return _summarize(item for item in self._records if item.run_id == run_id)

    def total_for_tenant(self, tenant_id: str) -> CostSummary:
        """聚合指定租户的全部成本。"""
        with self._lock:
            return _summarize(item for item in self._records if item.tenant_id == tenant_id)

    def summary(self) -> CostSummary:
        """聚合当前全部成本记录。"""
        with self._lock:
            return _summarize(self._records)


class CostTrackingHandler:
    """从生命周期事件中提取用量并写入 :class:`CostTracker` 的事件处理器。

    约定键名 ``tokens_input``、``tokens_output``、``cost_micro_units`` 和
    ``tool_calls``：优先读取最近一条迭代记录的执行结果 metadata，其次读取
    请求 metadata；全部缺失时直接跳过，不报错。建议只订阅
    ``EXECUTION_COMPLETED`` 等单次触发的事件类型，避免同一份用量被重复累计。
    """

    def __init__(self, tracker: CostTracker) -> None:
        self._tracker = tracker

    def __call__(self, event: LoopEvent) -> None:
        """尝试从事件中提取一条成本记录并写入累计器。"""
        source = _usage_source(event)
        if source is None:
            return
        tenant = source.get("tenant_id", event.context.request.metadata.get("tenant_id"))
        self._tracker.record(
            CostRecord(
                run_id=event.context.run_id,
                tenant_id=tenant if isinstance(tenant, str) else None,
                tokens_input=_usage_int(source, "tokens_input"),
                tokens_output=_usage_int(source, "tokens_output"),
                cost_micro_units=_usage_int(source, "cost_micro_units"),
                tool_calls=_usage_int(source, "tool_calls"),
                recorded_at=event.occurred_at,
            )
        )


def _summarize(records: Iterable[CostRecord]) -> CostSummary:
    """聚合任意成本记录集合。"""
    count = tokens_input = tokens_output = cost_micro_units = tool_calls = 0
    for record in records:
        count += 1
        tokens_input += record.tokens_input
        tokens_output += record.tokens_output
        cost_micro_units += record.cost_micro_units
        tool_calls += record.tool_calls
    return CostSummary(
        records=count,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_micro_units=cost_micro_units,
        tool_calls=tool_calls,
    )


def _usage_source(event: LoopEvent) -> Mapping[str, object] | None:
    """返回第一个包含约定用量键的 metadata 来源。"""
    candidates: list[Mapping[str, object]] = []
    if event.context.records:
        candidates.append(event.context.records[-1].execution.metadata)
    candidates.append(event.context.request.metadata)
    for candidate in candidates:
        if any(key in candidate for key in _USAGE_KEYS):
            return candidate
    return None


def _usage_int(source: Mapping[str, object], key: str) -> int:
    """读取一个非负整型用量字段，缺失或无效时按 0 处理。"""
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)
