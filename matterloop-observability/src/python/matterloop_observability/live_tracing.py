"""在 Loop 实际执行期间建立 OpenTelemetry 上下文。"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from matterloop_core import LoopContext, LoopEvent, LoopEventType

from matterloop_observability.redaction import Redactor

logger = logging.getLogger(__name__)

_TERMINAL_EVENTS = {
    LoopEventType.LOOP_COMPLETED,
    LoopEventType.LOOP_CANCELLED,
    LoopEventType.LOOP_TIMED_OUT,
    LoopEventType.LOOP_FAILED,
}

_SUSPENSION_EVENTS = {LoopEventType.LOOP_BLOCKED, LoopEventType.LOOP_PAUSED}


@dataclass(slots=True)
class _LiveSpan:
    """保存一个已附加到当前执行上下文的活动 OTel Span。"""

    span: Any
    token: Any
    name: str


@dataclass(slots=True)
class _LiveRun:
    """保存一次运行的根 Span 和可选阶段 Span。"""

    root: _LiveSpan
    phase: _LiveSpan | None = None


class OpenTelemetryTracePublisher:
    """用真实 OTel Span 表示 Loop 生命周期，并传播当前上下文。

    该发布器必须与数据库、HTTP 等自动 instrumentation 共用同一个
    ``TracerProvider``。Loop 调用外部组件前会先发布对应事件；asyncio 创建的
    子任务会继承该 ContextVar，因此数据库调用会自动成为当前运行/阶段 Span 的子节点。
    对于同一运行的一次连续执行，事件必须在创建该运行 Span 的同一 Task 中发布。阻塞或暂停前，
    发布器把当前根 Span 的 W3C 传播上下文写入同一次 checkpoint CAS；恢复会从该上下文创建真实
    子 Span，因此跨进程仍保持一条正确的 OTel Trace 树。
    """

    def __init__(self, tracer_provider: Any, redactor: Redactor | None = None) -> None:
        try:
            self._context = importlib.import_module("opentelemetry.context")
            self._propagate = importlib.import_module("opentelemetry.propagate")
            self._trace = importlib.import_module("opentelemetry.trace")
        except ImportError as exc:
            raise ImportError(
                "OpenTelemetryTracePublisher 需要 OpenTelemetry API，请安装 "
                "matterloop-observability[otel]"
            ) from exc
        self._tracer = tracer_provider.get_tracer("matterloop.observability")
        self._redactor = redactor or Redactor()
        self._runs: dict[str, _LiveRun] = {}

    async def prepare_checkpoint(
        self, context: LoopContext, event_types: tuple[LoopEventType, ...]
    ) -> None:
        """在 checkpoint 保存前持久化可恢复的 W3C 父上下文。"""
        try:
            if any(event_type in _TERMINAL_EVENTS for event_type in event_types):
                context.propagation_context.clear()
                return
            state = self._runs.get(context.run_id)
            if state is None:
                return
            carrier: dict[str, str] = {}
            parent_context = self._trace.set_span_in_context(state.root.span)
            self._propagate.inject(carrier, context=parent_context)
            if "traceparent" not in carrier:
                raise RuntimeError("OTel propagator did not inject traceparent")
            context.propagation_context.clear()
            context.propagation_context.update(
                {
                    header: carrier[header]
                    for header in ("traceparent", "tracestate")
                    if header in carrier
                }
            )
        except Exception:
            logger.exception("实时 OTel propagation context 持久化准备失败")

    async def publish(self, event: LoopEvent) -> None:
        """实现 Core EventPublisher，并且不让观测故障影响 Loop。"""
        try:
            self.handle(event)
        except Exception:
            logger.exception("实时 OTel 事件处理失败", extra={"run_id": event.context.run_id})

    def handle(self, event: LoopEvent) -> None:
        """同步消费生命周期事件，维护当前任务的 OTel context。"""
        run_id = event.context.run_id
        state = self._runs.get(run_id)
        if state is None:
            state = self._open_run(event)
            self._runs[run_id] = state
        if event.event_type in _SUSPENSION_EVENTS:
            self._close_run(run_id, state, event)
            return
        if event.event_type in _TERMINAL_EVENTS:
            self._close_run(run_id, state, event)
            return
        if event.event_type is LoopEventType.PLANNING_STARTED:
            self._open_phase(state, "planner", event)
        elif event.event_type is LoopEventType.PLAN_CREATED:
            self._close_phase(state, event)
        elif event.event_type is LoopEventType.EXECUTION_DISPATCHED:
            self._open_phase(state, "executor", event)
        elif event.event_type is LoopEventType.EXECUTION_COMPLETED:
            self._close_phase(state, event)
        elif event.event_type is LoopEventType.COMPONENT_RETRYING:
            self._close_phase(
                state,
                event,
                level="ERROR",
                status_message="组件调用失败，准备重试",
            )
        elif event.event_type is LoopEventType.VERIFICATION_STARTED:
            self._open_phase(state, "verifier", event)
        elif event.event_type is LoopEventType.ITERATION_COMPLETED:
            self._emit_verification_score(event)
            self._close_phase(state, event)
        elif event.event_type is LoopEventType.COMPLETION_EVALUATION_STARTED:
            self._open_phase(state, "completion_evaluator", event)
        elif event.event_type in {
            LoopEventType.COMPLETION_EVALUATION_COMPLETED,
            LoopEventType.COMPLETION_REPLAN_REQUESTED,
        }:
            self._close_phase(state, event)

    def _open_run(self, event: LoopEvent) -> _LiveRun:
        """创建并附加代表一次 Agent Run 的根 Span。"""
        options: dict[str, Any] = {"start_time": _nanoseconds(event.occurred_at)}
        parent_context = self._restored_parent_context(event.context)
        if parent_context is not None:
            options["context"] = parent_context
        span = self._tracer.start_span("matterloop.run", **options)
        span.set_attribute("matterloop.run_id", event.context.run_id)
        span.set_attribute("matterloop.goal", self._redact(event.context.request.goal))
        span.set_attribute("matterloop.status", event.context.status.value)
        token = self._context.attach(self._trace.set_span_in_context(span))
        return _LiveRun(root=_LiveSpan(span, token, "run"))

    def _restored_parent_context(self, context: LoopContext) -> Any | None:
        """从 checkpoint 恢复 W3C 传播上下文，作为恢复片段的真实父节点。"""
        if not context.propagation_context:
            return None
        try:
            restored = self._propagate.extract(
                {
                    header: context.propagation_context[header]
                    for header in ("traceparent", "tracestate")
                    if header in context.propagation_context
                }
            )
            span_context = self._trace.get_current_span(restored).get_span_context()
            if not span_context.is_valid:
                logger.warning("checkpoint 中的 OTel propagation context 无效，创建新的根 Span")
                return None
            return restored
        except Exception:
            logger.exception("checkpoint 中的 OTel propagation context 恢复失败")
            return None

    def _open_phase(self, state: _LiveRun, name: str, event: LoopEvent) -> None:
        """切换到一个实际执行阶段，供数据库/HTTP 子调用继承。"""
        if state.phase is not None:
            self._close_phase(
                state,
                event,
                level="ERROR",
                status_message="新的阶段开始前强制关闭上一阶段",
            )
        span = self._tracer.start_span(
            f"matterloop.{name}",
            start_time=_nanoseconds(event.occurred_at),
        )
        span.set_attribute("matterloop.run_id", event.context.run_id)
        if event.detail:
            span.set_attribute("matterloop.operation_id", event.detail)
        step = _current_step(event)
        if step is not None:
            span.set_attribute("matterloop.step_id", step.step_id)
            span.set_attribute("matterloop.executor", step.executor)
        token = self._context.attach(self._trace.set_span_in_context(span))
        state.phase = _LiveSpan(span, token, name)

    def _close_phase(
        self,
        state: _LiveRun,
        event: LoopEvent,
        *,
        level: str = "DEFAULT",
        status_message: str | None = None,
    ) -> None:
        """结束并从当前上下文移除活动阶段 Span。"""
        phase = state.phase
        if phase is None:
            return
        state.phase = None
        self._set_phase_result_attributes(phase, event)
        self._finish_span(phase, event, level=level, status_message=status_message)

    def _finish_span(
        self,
        active: _LiveSpan,
        event: LoopEvent,
        *,
        level: str = "DEFAULT",
        status_message: str | None = None,
    ) -> None:
        """无论解绑或属性写入是否失败都结束 Span。"""
        try:
            self._context.detach(active.token)
        except Exception:
            logger.exception("实时 OTel 上下文解绑失败", extra={"span_name": active.name})
        finally:
            try:
                if level == "ERROR":
                    active.span.set_status(
                        self._trace.Status(self._trace.StatusCode.ERROR, status_message)
                    )
            except Exception:
                logger.exception("实时 OTel Span 状态记录失败", extra={"span_name": active.name})
            finally:
                try:
                    active.span.end(end_time=_nanoseconds(event.occurred_at))
                except Exception:
                    logger.exception("实时 OTel Span 结束失败", extra={"span_name": active.name})

    def _set_phase_result_attributes(self, phase: _LiveSpan, event: LoopEvent) -> None:
        """把阶段结束时已知的业务结果附加到对应 Span。"""
        try:
            if phase.name == "executor":
                execution = event.context.pending_execution
                if execution is not None:
                    phase.span.set_attribute("matterloop.output", execution.output)
            elif phase.name == "verifier" and event.context.records:
                verification = event.context.records[-1].verification
                phase.span.set_attribute("matterloop.verification_passed", verification.passed)
                phase.span.set_attribute("matterloop.feedback", verification.feedback)
        except Exception:
            logger.exception("实时 OTel 阶段结果记录失败")

    def _emit_verification_score(self, event: LoopEvent) -> None:
        """把当前步骤验证分数写为 verifier Span 下的即时子 Span。"""
        if not event.context.records:
            return
        record = event.context.records[-1]
        raw_score = record.verification.score
        if raw_score is None:
            return
        score_span = self._tracer.start_span(
            "score:verification",
            start_time=_nanoseconds(event.occurred_at),
        )
        score_span.set_attribute("matterloop.run_id", event.context.run_id)
        score_span.set_attribute("matterloop.step_id", record.step.step_id)
        score_span.set_attribute("score.name", "verification")
        score_span.set_attribute("score.value", raw_score / 100.0)
        score_span.set_attribute("matterloop.score.raw_value", raw_score)
        score_span.set_attribute("score.source", "VERIFIER")
        score_span.set_attribute("matterloop.verification_passed", record.verification.passed)
        score_span.end(end_time=_nanoseconds(event.occurred_at))

    def _close_run(self, run_id: str, state: _LiveRun, event: LoopEvent) -> None:
        """结束全部打开阶段并关闭根 Span。"""
        failed = event.event_type is LoopEventType.LOOP_FAILED
        try:
            self._close_phase(
                state,
                event,
                level="ERROR" if failed else "DEFAULT",
                status_message=(
                    event.context.error or "运行结束时阶段被强制关闭"
                    if failed and state.phase is not None
                    else None
                ),
            )
        finally:
            try:
                try:
                    state.root.span.set_attribute("matterloop.status", event.context.status.value)
                except Exception:
                    logger.exception("实时 OTel 根 Span 状态记录失败")
                finally:
                    self._finish_span(
                        state.root,
                        event,
                        level="ERROR" if failed else "DEFAULT",
                        status_message=event.context.error or None if failed else None,
                    )
            finally:
                self._runs.pop(run_id, None)

    def _redact(self, value: str) -> str:
        """对根 Span 目标做与其他 observability 组件一致的键级脱敏。"""
        redacted = self._redactor.redact(value)
        return redacted if isinstance(redacted, str) else value


def _current_step(event: LoopEvent) -> Any | None:
    """读取事件时的当前计划步骤。"""
    plan = event.context.current_plan
    if plan is None:
        return None
    index = event.context.current_step_index
    return plan.steps[index] if 0 <= index < len(plan.steps) else None


def _nanoseconds(moment: datetime) -> int:
    """把时间转换为 OTel 所需的 Unix 纳秒。"""
    return int(moment.timestamp() * 1_000_000_000)
