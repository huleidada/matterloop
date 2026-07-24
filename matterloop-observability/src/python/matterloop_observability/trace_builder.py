"""把 Loop 生命周期事件流重建为树形跨度与评分的 TraceBuilder。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from matterloop_core import IterationRecord, LoopEvent, LoopEventType, PlanStep

from matterloop_observability.pipeline import BatchingPipeline
from matterloop_observability.redaction import Redactor
from matterloop_observability.scores import score_from_verification
from matterloop_observability.spans import SpanRecord, _OpenSpan

logger = logging.getLogger(__name__)

_TERMINAL_EVENTS = {
    LoopEventType.LOOP_COMPLETED,
    LoopEventType.LOOP_BLOCKED,
    LoopEventType.LOOP_CANCELLED,
    LoopEventType.LOOP_TIMED_OUT,
    LoopEventType.LOOP_FAILED,
}
"""标志一次运行结束、需要关闭全部未关跨度的事件。"""

_GOAL_NAME_LIMIT = 80
"""根跨度名称允许保留的目标摘要长度。"""


@dataclass(slots=True)
class _RunTrace:
    """跟踪一次运行中仍处于打开状态的角色跨度。"""

    root: _OpenSpan | None = None
    executor: _OpenSpan | None = None
    evaluator: _OpenSpan | None = None
    reviewer: _OpenSpan | None = None


class TraceBuilder:
    """实现 ``EventPublisher`` 协议，把事件流重建为树形 trace。

    每个 ``run_id`` 的跨度状态相互隔离并由锁保护，可安全承接并发运行；所有事件
    处理路径都做了防御性兜底，任何异常只会记录日志，不会传播到 Loop 主流程。
    """

    def __init__(
        self,
        pipeline: BatchingPipeline,
        redactor: Redactor | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._redactor = redactor or Redactor()
        self._lock = threading.Lock()
        self._runs: dict[str, _RunTrace] = {}

    @property
    def pipeline(self) -> BatchingPipeline:
        """返回跨度与评分写入的导出流水线。"""
        return self._pipeline

    async def publish(self, event: LoopEvent) -> None:
        """实现 ``EventPublisher`` 协议的异步入口。"""
        self.handle(event)

    def __call__(self, event: LoopEvent) -> None:
        """支持作为同步 ``EventHandler`` 直接挂接。"""
        self.handle(event)

    def handle(self, event: LoopEvent) -> None:
        """消费一个生命周期事件并更新跨度生命周期，绝不抛出。"""
        try:
            self._handle(event)
        except Exception:
            logger.exception(
                "追踪事件处理失败",
                extra={"run_id": getattr(event.context, "run_id", None)},
            )

    def resolve_parent_span_id(self, run_id: str, step_id: str | None = None) -> str | None:
        """返回指定运行中当前应作为 generation 跨度父节点的跨度标识。

        优先选择仍打开且 ``step_id`` 匹配的执行或评估跨度，其次回退到根跨度；
        运行不存在时返回 ``None``。
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            candidates = (state.evaluator, state.executor, state.reviewer)
            if step_id is not None:
                for span in candidates:
                    if span is not None and span.step_id == step_id:
                        return span.span_id
                # 指定的步骤没有打开的跨度时回退到根跨度，避免挂到无关步骤下。
                return state.root.span_id if state.root is not None else None
            for span in candidates:
                if span is not None:
                    return span.span_id
            return state.root.span_id if state.root is not None else None

    def _handle(self, event: LoopEvent) -> None:
        """按事件类型推进跨度生命周期。"""
        run_id = event.context.run_id
        with self._lock:
            state = self._runs.setdefault(run_id, _RunTrace())
            if event.event_type in _TERMINAL_EVENTS:
                self._close_run(state, run_id, event)
                self._runs.pop(run_id, None)
                return
            self._ensure_root(state, run_id, event)
            if event.event_type is LoopEventType.EXECUTION_DISPATCHED:
                self._open_executor(state, run_id, event)
            elif event.event_type is LoopEventType.EXECUTION_COMPLETED:
                self._close_executor(state, event)
            elif event.event_type is LoopEventType.COMPONENT_RETRYING:
                self._close_executor(
                    state,
                    event,
                    level="ERROR",
                    status_message="执行器调用失败，准备重试",
                )
            elif event.event_type is LoopEventType.VERIFICATION_STARTED:
                self._open_evaluator(state, run_id, event)
            elif event.event_type is LoopEventType.ITERATION_COMPLETED:
                self._complete_iteration(state, run_id, event)
            elif event.event_type is LoopEventType.COMPLETION_EVALUATION_STARTED:
                self._open_reviewer(state, run_id, event)
            elif event.event_type in {
                LoopEventType.COMPLETION_EVALUATION_COMPLETED,
                LoopEventType.COMPLETION_REPLAN_REQUESTED,
            }:
                self._close_role(state, "reviewer", event)

    def _ensure_root(self, state: _RunTrace, run_id: str, event: LoopEvent) -> None:
        """保证根跨度存在；错过 ``loop.started`` 时隐式补建。"""
        if state.root is not None:
            return
        goal = event.context.request.goal
        name = goal[:_GOAL_NAME_LIMIT] if goal.strip() else run_id
        state.root = _OpenSpan(
            trace_id=run_id,
            span_id=uuid4().hex,
            parent_span_id=None,
            name=name,
            observation_type="chain",
            started_at=event.occurred_at,
            attributes=self._redact(
                {
                    "matterloop.run_id": run_id,
                    "matterloop.goal": goal,
                    "matterloop.status": event.context.status.value,
                }
            ),
        )

    def _current_step(self, event: LoopEvent) -> PlanStep | None:
        """防御性地读取事件发生时正在执行的计划步骤。"""
        plan = event.context.current_plan
        if plan is None:
            return None
        index = event.context.current_step_index
        if not 0 <= index < len(plan.steps):
            return None
        return plan.steps[index]

    def _open_executor(self, state: _RunTrace, run_id: str, event: LoopEvent) -> None:
        """为一次执行器调用打开跨度。"""
        if state.executor is not None:
            # 事件流若遗漏重试通知，仍不能覆盖未关闭的旧尝试。
            self._close_executor(
                state,
                event,
                level="ERROR",
                status_message="新的执行尝试开始前强制关闭上一尝试",
            )
        step = self._current_step(event)
        step_id = step.step_id if step is not None else None
        name = f"executor:{step.executor}" if step is not None else "executor"
        attributes: dict[str, Any] = {"matterloop.run_id": run_id}
        if step_id is not None:
            attributes["matterloop.step_id"] = step_id
            attributes["matterloop.step_description"] = step.description if step else ""
        if event.detail:
            attributes["matterloop.operation_id"] = event.detail
        attributes["matterloop.attempt"] = event.context.pending_attempt or (
            event.context.total_attempts
        )
        state.executor = _OpenSpan(
            trace_id=run_id,
            span_id=uuid4().hex,
            parent_span_id=state.root.span_id if state.root else None,
            name=name,
            observation_type="span",
            started_at=event.occurred_at,
            attributes=self._redact(attributes),
            step_id=step_id,
        )

    def _close_executor(
        self,
        state: _RunTrace,
        event: LoopEvent,
        *,
        level: str = "DEFAULT",
        status_message: str | None = None,
    ) -> None:
        """关闭执行器跨度并附带脱敏后的执行输出。"""
        span = state.executor
        if span is None:
            return
        state.executor = None
        extra: dict[str, Any] = {}
        pending = event.context.pending_execution
        if pending is not None:
            extra["matterloop.output"] = pending.output
        self._emit(
            span.close(
                event.occurred_at,
                level=level,
                status_message=status_message,
                extra_attributes=self._redact(extra),
            )
        )

    def _open_evaluator(self, state: _RunTrace, run_id: str, event: LoopEvent) -> None:
        """为一次步骤验证打开评估跨度。"""
        step = self._current_step(event)
        step_id = step.step_id if step is not None else None
        attributes: dict[str, Any] = {"matterloop.run_id": run_id}
        if step_id is not None:
            attributes["matterloop.step_id"] = step_id
        state.evaluator = _OpenSpan(
            trace_id=run_id,
            span_id=uuid4().hex,
            parent_span_id=state.root.span_id if state.root else None,
            name="verifier",
            observation_type="evaluator",
            started_at=event.occurred_at,
            attributes=self._redact(attributes),
            step_id=step_id,
        )

    def _complete_iteration(self, state: _RunTrace, run_id: str, event: LoopEvent) -> None:
        """关闭评估跨度、记录迭代快照跨度并提取验证评分。"""
        record = event.context.records[-1] if event.context.records else None
        step_id = record.step.step_id if record is not None else None
        evaluator = state.evaluator
        if evaluator is not None:
            state.evaluator = None
            extra: dict[str, Any] = {}
            if record is not None:
                extra["matterloop.verification_passed"] = record.verification.passed
                extra["matterloop.feedback"] = record.verification.feedback
            self._emit(evaluator.close(event.occurred_at, extra_attributes=self._redact(extra)))
        if record is not None:
            self._emit(self._iteration_span(state, run_id, record, event.occurred_at))
            score = score_from_verification(run_id, step_id, record.verification)
            if score is not None:
                self._emit(score)

    def _iteration_span(
        self,
        state: _RunTrace,
        run_id: str,
        record: IterationRecord,
        moment: datetime,
    ) -> SpanRecord:
        """把已完成的迭代证据转换为一个瞬时快照跨度。"""
        attributes = self._redact(
            {
                "matterloop.run_id": run_id,
                "matterloop.step_id": record.step.step_id,
                "matterloop.cycle": record.cycle,
                "matterloop.step_index": record.step_index,
                "matterloop.attempt": record.attempt,
                "matterloop.verification_passed": record.verification.passed,
                "matterloop.output": record.execution.output,
            }
        )
        return _OpenSpan(
            trace_id=run_id,
            span_id=uuid4().hex,
            parent_span_id=state.root.span_id if state.root else None,
            name=f"iteration:c{record.cycle}:s{record.step_index}",
            observation_type="span",
            started_at=moment,
            attributes=attributes,
            step_id=record.step.step_id,
        ).close(moment)

    def _open_reviewer(self, state: _RunTrace, run_id: str, event: LoopEvent) -> None:
        """为一次整体完成度评估打开审查跨度。"""
        state.reviewer = _OpenSpan(
            trace_id=run_id,
            span_id=uuid4().hex,
            parent_span_id=state.root.span_id if state.root else None,
            name="completion_evaluator",
            observation_type="evaluator",
            started_at=event.occurred_at,
            attributes=self._redact({"matterloop.run_id": run_id}),
        )

    def _close_role(self, state: _RunTrace, role: str, event: LoopEvent) -> None:
        """关闭一个指定角色的打开跨度。"""
        span = getattr(state, role)
        if span is None:
            return
        setattr(state, role, None)
        self._emit(span.close(event.occurred_at))

    def _close_run(self, state: _RunTrace, run_id: str, event: LoopEvent) -> None:
        """在终态事件中强制关闭全部未关跨度并关闭根跨度。"""
        failed = event.event_type is LoopEventType.LOOP_FAILED
        error = event.context.error or None
        for role in ("evaluator", "executor", "reviewer"):
            span = getattr(state, role)
            if span is None:
                continue
            setattr(state, role, None)
            self._emit(
                span.close(
                    event.occurred_at,
                    level="ERROR" if failed else "DEFAULT",
                    status_message="运行结束时跨度被强制关闭",
                )
            )
        if state.root is None:
            self._ensure_root(state, run_id, event)
        assert state.root is not None
        self._emit(
            state.root.close(
                event.occurred_at,
                level="ERROR" if failed else "DEFAULT",
                status_message=error if failed else None,
            )
        )
        state.root = None

    def _redact(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """对跨度属性做防御性脱敏，脱敏失败时保留原值。"""
        try:
            redacted = self._redactor.redact(attributes)
            return dict(redacted) if isinstance(redacted, dict) else attributes
        except Exception:
            logger.exception("跨度属性脱敏失败")
            return attributes

    def _emit(self, item: Any) -> None:
        """把完成的跨度或评分送入导出流水线。"""
        self._pipeline.enqueue(item)
