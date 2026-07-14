"""提供多 scope、原子预留的本地计算额度账本。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from matterloop_policies.errors import ResourceLimitExceededError, UsageReservationError

if TYPE_CHECKING:
    from matterloop_policies.budget import BudgetLimits


def _freeze_costs(costs: Mapping[str, int]) -> Mapping[str, int]:
    """规范币种并返回只读费用映射。"""
    normalized: dict[str, int] = {}
    for raw_currency, value in costs.items():
        currency = raw_currency.strip().upper()
        if not currency:
            raise ValueError("currency must not be empty")
        if value < 0:
            raise ValueError("cost must not be negative")
        if value:
            normalized[currency] = normalized.get(currency, 0) + value
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class UsageAmount:
    """描述一次申请或结算包含的全部可计量资源。

    ``cache_*`` 与 ``reasoning_tokens`` 是 ``input_tokens``、``output_tokens`` 的
    明细，不会被重复加入 ``total_tokens``。
    """

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: int = 0
    agent_tasks: int = 0
    attempts: int = 0
    costs_micros: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝负数用量并冻结费用映射。"""
        values = (
            self.model_calls,
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cache_hit_tokens,
            self.cache_miss_tokens,
            self.reasoning_tokens,
            self.tool_calls,
            self.agent_tasks,
            self.attempts,
        )
        if min(values, default=0) < 0:
            raise ValueError("usage values must not be negative")
        object.__setattr__(self, "costs_micros", _freeze_costs(self.costs_micros))

    def cost_for(self, currency: str) -> int:
        """返回指定币种的 micro-unit 费用。"""
        return self.costs_micros.get(currency.strip().upper(), 0)

    @property
    def cost_micros(self) -> int:
        """兼容旧 API，返回 USD 的 micro-dollar 费用。"""
        return self.cost_for("USD")

    @property
    def is_zero(self) -> bool:
        """判断该用量是否没有占用任何资源。"""
        return not any(
            (
                self.model_calls,
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
                self.cache_hit_tokens,
                self.cache_miss_tokens,
                self.reasoning_tokens,
                self.tool_calls,
                self.agent_tasks,
                self.attempts,
                *self.costs_micros.values(),
            )
        )


@dataclass(frozen=True, slots=True)
class UsageSnapshot(UsageAmount):
    """表示一个 scope 已结算、预留和并发中的资源快照。"""

    active_model_calls: int = 0
    reserved: UsageAmount = field(default_factory=UsageAmount)

    def __post_init__(self) -> None:
        """复用基础校验并拒绝负数并发计数。"""
        UsageAmount.__post_init__(self)
        if self.active_model_calls < 0:
            raise ValueError("active model calls must not be negative")


@dataclass(frozen=True, slots=True)
class UsageReservation:
    """标识一笔尚未 commit 或 rollback 的原子额度预留。"""

    reservation_id: str
    scopes: tuple[str, ...]
    amount: UsageAmount


@dataclass(slots=True)
class _ScopeState:
    """账本内部的单 scope 可变状态。"""

    committed: UsageAmount = field(default_factory=UsageAmount)
    reserved: UsageAmount = field(default_factory=UsageAmount)
    active_model_calls: int = 0


class UsageLedger:
    """提供线程安全、多 scope 且不会超卖的本地资源账本。

    同一次调用可以传入 team、child loop、task 和 agent 等多个 scope。账本会先
    校验全部 scope，再一次性写入全部 scope；任何一个失败都不会产生部分预留。
    """

    def __init__(self, default_limits: BudgetLimits | None = None) -> None:
        self._default_limits = default_limits
        self._scope_limits: dict[str, BudgetLimits] = {}
        self._states: dict[str, _ScopeState] = {}
        self._reservations: dict[str, UsageReservation] = {}
        self._lock = RLock()

    def configure_scope(self, scope: str, limits: BudgetLimits) -> None:
        """为一个 scope 设置硬上限，并立即校验现有占用。"""
        normalized = _normalize_scope(scope)
        with self._lock:
            state = self._states.get(normalized, _ScopeState())
            self._check_limits(normalized, state, UsageAmount(), limits)
            self._scope_limits[normalized] = limits

    def limits_for(self, scope: str) -> BudgetLimits | None:
        """返回 scope 的显式上限或默认上限。"""
        normalized = _normalize_scope(scope)
        with self._lock:
            return self._scope_limits.get(normalized, self._default_limits)

    def has_cost_limit(self, scopes: str | Iterable[str]) -> bool:
        """判断任一目标 scope 是否配置了费用硬上限。"""
        return bool(self.cost_limit_currencies(scopes))

    def cost_limit_currencies(self, scopes: str | Iterable[str]) -> frozenset[str]:
        """返回目标 scopes 中所有启用费用上限的币种。"""
        normalized = _normalize_scopes(scopes)
        with self._lock:
            return frozenset(
                limits.cost_currency
                for scope in normalized
                if (limits := self._scope_limits.get(scope, self._default_limits)) is not None
                and limits.max_cost_micros is not None
            )

    def reserve(
        self,
        scopes: str | Iterable[str],
        amount: UsageAmount,
        *,
        reservation_id: str | None = None,
    ) -> UsageReservation:
        """在全部 scope 上原子预留资源。

        模型预留中的 ``model_calls`` 同时表示活跃模型调用数，因此并发上限也在该
        临界区内检查。
        """
        normalized_scopes = _normalize_scopes(scopes)
        if amount.is_zero:
            raise ValueError("reservation amount must not be empty")
        identifier = reservation_id or uuid4().hex
        if not identifier.strip():
            raise ValueError("reservation id must not be empty")
        with self._lock:
            if identifier in self._reservations:
                raise UsageReservationError("reservation id already exists")
            for scope in normalized_scopes:
                state = self._states.get(scope, _ScopeState())
                limits = self._scope_limits.get(scope, self._default_limits)
                if limits is not None:
                    self._check_limits(scope, state, amount, limits)
            reservation = UsageReservation(identifier, normalized_scopes, amount)
            for scope in normalized_scopes:
                state = self._states.setdefault(scope, _ScopeState())
                state.reserved = _add(state.reserved, amount)
                state.active_model_calls += amount.model_calls
            self._reservations[identifier] = reservation
            return reservation

    def commit(
        self,
        reservation: UsageReservation,
        actual: UsageAmount | None = None,
    ) -> None:
        """释放预留并把供应商实际用量原子结算到全部 scope。

        正常情况下实际用量小于保守预留。如果供应商报告的实际值意外超过预留，账本
        仍会如实记录已经发生的消耗，再抛出 ``ResourceLimitExceededError``，从而
        阻止后续调用并避免审计数据丢失。
        """
        actual_amount = actual or reservation.amount
        with self._lock:
            stored = self._require_reservation(reservation)
            for scope in stored.scopes:
                state = self._states[scope]
                state.reserved = _subtract(state.reserved, stored.amount)
                state.active_model_calls -= stored.amount.model_calls
                state.committed = _add(state.committed, actual_amount)
            del self._reservations[stored.reservation_id]
            violation = self._first_current_violation(stored.scopes)
            if violation is not None:
                raise violation

    def rollback(self, reservation: UsageReservation) -> None:
        """原子释放一笔失败调用的全部预留。"""
        with self._lock:
            stored = self._require_reservation(reservation)
            for scope in stored.scopes:
                state = self._states[scope]
                state.reserved = _subtract(state.reserved, stored.amount)
                state.active_model_calls -= stored.amount.model_calls
            del self._reservations[stored.reservation_id]

    def consume(self, scopes: str | Iterable[str], amount: UsageAmount) -> None:
        """对无需跨 await 持有预留的计数执行原子校验与结算。"""
        normalized_scopes = _normalize_scopes(scopes)
        if amount.is_zero:
            return
        with self._lock:
            for scope in normalized_scopes:
                state = self._states.get(scope, _ScopeState())
                limits = self._scope_limits.get(scope, self._default_limits)
                if limits is not None:
                    self._check_limits(scope, state, amount, limits)
            for scope in normalized_scopes:
                state = self._states.setdefault(scope, _ScopeState())
                state.committed = _add(state.committed, amount)

    def record_model_usage(
        self,
        scopes: str | Iterable[str],
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int | None = None,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_micros: int = 0,
        currency: str = "USD",
    ) -> None:
        """兼容旧调用方式，原子累计一次已完成模型调用。"""
        self.consume(
            scopes,
            UsageAmount(
                model_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=(
                    input_tokens + output_tokens if total_tokens is None else total_tokens
                ),
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                reasoning_tokens=reasoning_tokens,
                costs_micros={currency: cost_micros},
            ),
        )

    def record_tool_call(self, scopes: str | Iterable[str]) -> None:
        """原子累计一次已完成工具调用。"""
        self.consume(scopes, UsageAmount(tool_calls=1))

    def record_agent_task(self, scopes: str | Iterable[str]) -> None:
        """原子累计一次已完成 Agent 任务。"""
        self.consume(scopes, UsageAmount(agent_tasks=1))

    def record_attempt(self, scopes: str | Iterable[str]) -> None:
        """原子累计一次已完成 Executor 尝试。"""
        self.consume(scopes, UsageAmount(attempts=1))

    def snapshot(self, scope: str) -> UsageSnapshot:
        """返回指定 scope 的不可变用量快照。"""
        normalized = _normalize_scope(scope)
        with self._lock:
            state = self._states.get(normalized, _ScopeState())
            current = state.committed
            return UsageSnapshot(
                model_calls=current.model_calls,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                total_tokens=current.total_tokens,
                cache_hit_tokens=current.cache_hit_tokens,
                cache_miss_tokens=current.cache_miss_tokens,
                reasoning_tokens=current.reasoning_tokens,
                tool_calls=current.tool_calls,
                agent_tasks=current.agent_tasks,
                attempts=current.attempts,
                costs_micros=current.costs_micros,
                active_model_calls=state.active_model_calls,
                reserved=state.reserved,
            )

    def clear(self, scope: str) -> None:
        """清理已经结束且没有活跃预留的 scope。"""
        normalized = _normalize_scope(scope)
        with self._lock:
            state = self._states.get(normalized)
            if state is not None and state.active_model_calls:
                raise UsageReservationError("cannot clear a scope with active reservations")
            if any(normalized in item.scopes for item in self._reservations.values()):
                raise UsageReservationError("cannot clear a scope with active reservations")
            self._states.pop(normalized, None)

    def _require_reservation(self, reservation: UsageReservation) -> UsageReservation:
        stored = self._reservations.get(reservation.reservation_id)
        if stored is None or stored != reservation:
            raise UsageReservationError("reservation is unknown or already finalized")
        return stored

    def _first_current_violation(
        self,
        scopes: tuple[str, ...],
    ) -> ResourceLimitExceededError | None:
        for scope in scopes:
            limits = self._scope_limits.get(scope, self._default_limits)
            if limits is None:
                continue
            state = self._states[scope]
            try:
                self._check_limits(scope, state, UsageAmount(), limits)
            except ResourceLimitExceededError as error:
                return error
        return None

    @staticmethod
    def _check_limits(
        scope: str,
        state: _ScopeState,
        requested: UsageAmount,
        limits: BudgetLimits,
    ) -> None:
        projected = _add(_add(state.committed, state.reserved), requested)
        checks = (
            ("model_calls", projected.model_calls, limits.max_model_calls),
            (
                "concurrent_model_calls",
                state.active_model_calls + requested.model_calls,
                limits.max_concurrent_model_calls,
            ),
            ("input_tokens", projected.input_tokens, limits.max_input_tokens),
            ("output_tokens", projected.output_tokens, limits.max_output_tokens),
            ("total_tokens", projected.total_tokens, limits.max_total_tokens),
            (
                "cache_hit_tokens",
                projected.cache_hit_tokens,
                limits.max_cache_hit_tokens,
            ),
            (
                "cache_miss_tokens",
                projected.cache_miss_tokens,
                limits.max_cache_miss_tokens,
            ),
            ("reasoning_tokens", projected.reasoning_tokens, limits.max_reasoning_tokens),
            ("tool_calls", projected.tool_calls, limits.max_tool_calls),
            ("agent_tasks", projected.agent_tasks, limits.max_agent_tasks),
            ("attempts", projected.attempts, limits.max_attempts),
            (
                f"cost_micros:{limits.cost_currency}",
                projected.cost_for(limits.cost_currency),
                limits.max_cost_micros,
            ),
        )
        current = _add(state.committed, state.reserved)
        for resource, value, limit in checks:
            if limit is not None and value > limit:
                current_value = (
                    current.cost_for(limits.cost_currency)
                    if resource.startswith("cost_micros:")
                    else (
                        state.active_model_calls
                        if resource == "concurrent_model_calls"
                        else int(getattr(current, resource))
                    )
                )
                requested_value = value - current_value
                raise ResourceLimitExceededError(
                    scope=scope,
                    resource=resource,
                    limit=limit,
                    current=current_value,
                    requested=requested_value,
                )


def _normalize_scope(scope: str) -> str:
    normalized = scope.strip()
    if not normalized:
        raise ValueError("usage scope must not be empty")
    return normalized


def _normalize_scopes(scopes: str | Iterable[str]) -> tuple[str, ...]:
    values = (scopes,) if isinstance(scopes, str) else tuple(scopes)
    if not values:
        raise ValueError("at least one usage scope is required")
    normalized = tuple(_normalize_scope(scope) for scope in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("usage scopes must not contain duplicates")
    return normalized


def _add(left: UsageAmount, right: UsageAmount) -> UsageAmount:
    currencies = set(left.costs_micros) | set(right.costs_micros)
    return UsageAmount(
        model_calls=left.model_calls + right.model_calls,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cache_hit_tokens=left.cache_hit_tokens + right.cache_hit_tokens,
        cache_miss_tokens=left.cache_miss_tokens + right.cache_miss_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        tool_calls=left.tool_calls + right.tool_calls,
        agent_tasks=left.agent_tasks + right.agent_tasks,
        attempts=left.attempts + right.attempts,
        costs_micros={
            currency: left.cost_for(currency) + right.cost_for(currency) for currency in currencies
        },
    )


def _subtract(left: UsageAmount, right: UsageAmount) -> UsageAmount:
    currencies = set(left.costs_micros) | set(right.costs_micros)
    return UsageAmount(
        model_calls=left.model_calls - right.model_calls,
        input_tokens=left.input_tokens - right.input_tokens,
        output_tokens=left.output_tokens - right.output_tokens,
        total_tokens=left.total_tokens - right.total_tokens,
        cache_hit_tokens=left.cache_hit_tokens - right.cache_hit_tokens,
        cache_miss_tokens=left.cache_miss_tokens - right.cache_miss_tokens,
        reasoning_tokens=left.reasoning_tokens - right.reasoning_tokens,
        tool_calls=left.tool_calls - right.tool_calls,
        agent_tasks=left.agent_tasks - right.agent_tasks,
        attempts=left.attempts - right.attempts,
        costs_micros={
            currency: left.cost_for(currency) - right.cost_for(currency) for currency in currencies
        },
    )


__all__ = ["UsageAmount", "UsageLedger", "UsageReservation", "UsageSnapshot"]
