"""提供显式价格表与模型调用预算代理。"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from threading import RLock
from typing import Protocol, runtime_checkable

from matterloop_models import (
    ModelClient,
    ModelRequest,
    ModelResponse,
    ModelResponseParseError,
    TokenUsage,
)

from matterloop_policies.errors import BudgetConfigurationError
from matterloop_policies.usage import UsageAmount, UsageLedger, UsageReservation

_MICROS_PER_MILLION = 1_000_000
_MAX_TRACKED_CONTINUATIONS = 1024


@dataclass(frozen=True, slots=True)
class TokenRateCard:
    """定义调用方显式注入的每百万 Token 整数费率。

    Args:
        currency: 费用币种，例如 ``USD``。
        effective_from: 此价格表开始生效的自然日。
        input_micros_per_million: 普通输入 Token 的 micro-unit 费率。
        output_micros_per_million: 普通输出 Token 的 micro-unit 费率。
        cache_hit_input_micros_per_million: 缓存命中输入的可选专用费率。
        cache_miss_input_micros_per_million: 缓存未命中输入的可选专用费率。
        reasoning_output_micros_per_million: reasoning 输出的可选专用费率。

    价格表没有供应商默认值，MatterLoop 也不会联网查询价格。
    """

    currency: str
    effective_from: date
    input_micros_per_million: int
    output_micros_per_million: int
    cache_hit_input_micros_per_million: int | None = None
    cache_miss_input_micros_per_million: int | None = None
    reasoning_output_micros_per_million: int | None = None

    def __post_init__(self) -> None:
        """规范币种并拒绝负数费率。"""
        currency = self.currency.strip().upper()
        if not currency:
            raise ValueError("rate card currency must not be empty")
        object.__setattr__(self, "currency", currency)
        rates = (
            self.input_micros_per_million,
            self.output_micros_per_million,
            self.cache_hit_input_micros_per_million,
            self.cache_miss_input_micros_per_million,
            self.reasoning_output_micros_per_million,
        )
        if any(rate is not None and rate < 0 for rate in rates):
            raise ValueError("token rates must not be negative")

    def calculate_cost(self, usage: TokenUsage) -> int:
        """按实际 Token 明细计算向上取整的 micro-unit 费用。"""
        cache_hit = getattr(usage, "cache_hit_tokens", 0)
        cache_miss = getattr(usage, "cache_miss_tokens", 0)
        detailed_input = cache_hit + cache_miss
        ordinary_input = max(0, usage.input_tokens - detailed_input)
        reasoning = min(getattr(usage, "reasoning_tokens", 0), usage.output_tokens)
        ordinary_output = max(0, usage.output_tokens - reasoning)
        numerator = ordinary_input * self.input_micros_per_million
        numerator += cache_hit * self._cache_hit_rate
        numerator += cache_miss * self._cache_miss_rate
        numerator += ordinary_output * self.output_micros_per_million
        numerator += reasoning * self._reasoning_rate
        return _ceil_div(numerator, _MICROS_PER_MILLION)

    def estimate_max_cost(self, *, input_tokens: int, output_tokens: int) -> int:
        """按各侧最高费率计算调用前的保守费用预留。"""
        if min(input_tokens, output_tokens) < 0:
            raise ValueError("estimated token counts must not be negative")
        input_rate = max(
            self.input_micros_per_million,
            self._cache_hit_rate,
            self._cache_miss_rate,
        )
        output_rate = max(self.output_micros_per_million, self._reasoning_rate)
        numerator = input_tokens * input_rate + output_tokens * output_rate
        return _ceil_div(numerator, _MICROS_PER_MILLION)

    @property
    def _cache_hit_rate(self) -> int:
        return (
            self.input_micros_per_million
            if self.cache_hit_input_micros_per_million is None
            else self.cache_hit_input_micros_per_million
        )

    @property
    def _cache_miss_rate(self) -> int:
        return (
            self.input_micros_per_million
            if self.cache_miss_input_micros_per_million is None
            else self.cache_miss_input_micros_per_million
        )

    @property
    def _reasoning_rate(self) -> int:
        return (
            self.output_micros_per_million
            if self.reasoning_output_micros_per_million is None
            else self.reasoning_output_micros_per_million
        )


@runtime_checkable
class ModelInputTokenEstimator(Protocol):
    """估算模型请求输入 Token 上界的结构协议。"""

    def __call__(self, request: ModelRequest) -> int:
        """返回大于等于一的保守输入 Token 估算。"""
        ...


def estimate_utf8_input_tokens(request: ModelRequest) -> int:
    """以请求可见 UTF-8 字节数作为无供应商 tokenizer 时的保守上界。

    函数只返回长度，不保存或记录消息内容，也不会检查 continuation、metadata 或凭据。
    调用方可注入供应商 tokenizer 获得更紧的预留值。
    """
    size = 0
    for message in request.messages:
        size += len(message.role.value.encode("utf-8"))
        size += len(message.content.encode("utf-8"))
        if message.name is not None:
            size += len(message.name.encode("utf-8"))
        size += 8
    for tool in request.tools:
        size += len(tool.name.encode("utf-8"))
        size += len(tool.description.encode("utf-8"))
        size += len(
            json.dumps(
                dict(tool.parameters),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    for output in request.tool_outputs:
        size += len(output.call_id.encode("utf-8"))
        size += len(output.output.encode("utf-8"))
        size += 8
    if request.response_schema is not None:
        size += len(
            json.dumps(
                dict(request.response_schema),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return max(1, size)


class BudgetedModelClient:
    """在调用已构造的模型客户端前执行本地额度预留。

    代理不构造供应商 SDK、不读取环境变量，也不持有 API key。一次 ``generate``
    会在 await 前预留，供应商异常或取消时回滚，成功后按响应中的实际 usage 结算。
    """

    def __init__(
        self,
        client: ModelClient,
        ledger: UsageLedger,
        *,
        rate_card: TokenRateCard | None = None,
        default_scopes: Iterable[str] = ("global",),
        default_max_output_tokens: int = 4096,
        input_token_estimator: ModelInputTokenEstimator | None = None,
    ) -> None:
        if default_max_output_tokens < 1:
            raise ValueError("default max output tokens must be at least 1")
        scopes = tuple(scope.strip() for scope in default_scopes)
        if not scopes or any(not scope for scope in scopes):
            raise ValueError("default usage scopes must not be empty")
        if len(scopes) != len(set(scopes)):
            raise ValueError("default usage scopes must not contain duplicates")
        self._client = client
        self._ledger = ledger
        self._rate_card = rate_card
        self._default_scopes = scopes
        self._default_max_output_tokens = default_max_output_tokens
        self._input_token_estimator = input_token_estimator or estimate_utf8_input_tokens
        self._uses_default_estimator = input_token_estimator is None
        self._continuation_estimates: OrderedDict[int, tuple[object, int]] = OrderedDict()
        self._continuation_lock = RLock()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """预留模型调用、Token、并发和费用后执行一次模型请求。"""
        request_scopes = getattr(request, "usage_scopes", ())
        scopes = request_scopes or self._default_scopes
        cost_currencies = self._ledger.cost_limit_currencies(scopes)
        if self._rate_card is None and cost_currencies:
            raise BudgetConfigurationError(
                "a TokenRateCard is required when a cost limit is configured"
            )
        if self._rate_card is not None and cost_currencies - {self._rate_card.currency}:
            raise BudgetConfigurationError(
                "rate card currency must match every configured cost limit"
            )
        estimated_input = self._input_token_estimator(request)
        if estimated_input < 1:
            raise BudgetConfigurationError("input token estimator must return at least 1")
        if self._uses_default_estimator and request.continuation is not None:
            with self._continuation_lock:
                tracked = self._continuation_estimates.get(id(request.continuation))
            if tracked is None or tracked[0] is not request.continuation:
                raise BudgetConfigurationError(
                    "opaque continuation must originate from this budgeted client "
                    "or use a custom token estimator"
                )
            estimated_input += tracked[1]
        estimated_output = request.max_output_tokens or self._default_max_output_tokens
        estimated_cost = (
            0
            if self._rate_card is None
            else self._rate_card.estimate_max_cost(
                input_tokens=estimated_input,
                output_tokens=estimated_output,
            )
        )
        reservation = self._ledger.reserve(
            scopes,
            UsageAmount(
                model_calls=1,
                input_tokens=estimated_input,
                output_tokens=estimated_output,
                total_tokens=estimated_input + estimated_output,
                # 未调用供应商前无法知道缓存与 reasoning 分布，各自按最坏情况预留。
                cache_hit_tokens=estimated_input,
                cache_miss_tokens=estimated_input,
                reasoning_tokens=estimated_output,
                costs_micros=(
                    {} if self._rate_card is None else {self._rate_card.currency: estimated_cost}
                ),
            ),
        )
        try:
            response = await self._client.generate(request)
        except ModelResponseParseError as error:
            if error.usage is None:
                self._ledger.rollback(reservation)
            else:
                self._settle_usage(reservation, error.usage)
            raise
        except BaseException:
            self._ledger.rollback(reservation)
            raise
        total_tokens = self._settle_usage(reservation, response.usage)
        if self._uses_default_estimator:
            with self._continuation_lock:
                if request.continuation is not None:
                    self._continuation_estimates.pop(id(request.continuation), None)
                if response.continuation is not None:
                    self._continuation_estimates[id(response.continuation)] = (
                        response.continuation,
                        total_tokens,
                    )
                    self._continuation_estimates.move_to_end(id(response.continuation))
                while len(self._continuation_estimates) > _MAX_TRACKED_CONTINUATIONS:
                    self._continuation_estimates.popitem(last=False)
        return response

    def _settle_usage(
        self,
        reservation: UsageReservation,
        usage: TokenUsage,
    ) -> int:
        """按实际 Token 与显式价格表结算一次已经发生的远端调用。"""
        total_tokens = usage.total_tokens or usage.input_tokens + usage.output_tokens
        actual_cost = 0 if self._rate_card is None else self._rate_card.calculate_cost(usage)
        self._ledger.commit(
            reservation,
            UsageAmount(
                model_calls=1,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=total_tokens,
                cache_hit_tokens=usage.cache_hit_tokens,
                cache_miss_tokens=usage.cache_miss_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                costs_micros=(
                    {} if self._rate_card is None else {self._rate_card.currency: actual_cost}
                ),
            ),
        )
        return total_tokens


def _ceil_div(numerator: int, denominator: int) -> int:
    if numerator == 0:
        return 0
    return (numerator + denominator - 1) // denominator


__all__ = [
    "BudgetedModelClient",
    "ModelInputTokenEstimator",
    "TokenRateCard",
    "estimate_utf8_input_tokens",
]
