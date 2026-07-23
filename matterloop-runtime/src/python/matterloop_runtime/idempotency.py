"""幂等控制：请求规范化哈希与结果复用调用器。

``canonical_request_hash`` 通过确定性 JSON 序列化生成幂等键，键序无关；
``IdempotentInvoker`` 借助执行账本在重复请求时直接复用已提交结果，并在检测到
未完成的同键执行时抛出异常，绝不自动重放外部副作用。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from matterloop_runtime.errors import RuntimeErrorBase
from matterloop_runtime.ledger import (
    ExecutionLedger,
    ExecutionLedgerError,
    ExecutionRecord,
    ExecutionStatus,
)

ExecutionCallable = Callable[[], Awaitable[str]]
"""执行外部副作用并返回可缓存字符串结果的异步回调。"""


class DuplicateExecutionError(RuntimeErrorBase):
    """同一请求已存在不可直接复用的执行记录，拒绝自动重放。

    Attributes:
        execution_id: 冲突记录的执行标识，供宿主查询或对账。
        status: 冲突记录当前状态。
    """

    def __init__(self, execution_id: str, status: ExecutionStatus) -> None:
        """初始化异常。

        Args:
            execution_id: 冲突记录的执行标识。
            status: 冲突记录当前状态。
        """
        super().__init__(
            f"execution already exists and cannot be replayed: {execution_id} ({status.value})"
        )
        self.execution_id = execution_id
        self.status = status


def canonical_request_hash(operation: str, arguments: Mapping[str, object]) -> str:
    """计算与参数键序无关的确定性请求哈希。

    参数会被递归规范化后按排序键序列化为 JSON（``ensure_ascii=False``），再取
    SHA-256 十六进制摘要。

    Args:
        operation: 操作名称，不允许为空。
        arguments: 请求参数；嵌套值只允许 JSON 可表达的标量、映射和序列。

    Returns:
        64 位十六进制哈希字符串。

    Raises:
        ValueError: 操作名为空，或参数包含不可确定性序列化的值。
    """
    if not operation:
        raise ValueError("operation must not be empty")
    payload = {"operation": operation, "arguments": _canonicalize(arguments)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonicalize(value: object) -> object:
    """递归规范化参数值；无法确定性序列化时抛出 ``ValueError``。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("arguments must not contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        canonical: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("argument mapping keys must be strings")
            canonical[key] = _canonicalize(item)
        return canonical
    if isinstance(value, (bytes, bytearray)):
        raise ValueError("arguments must not contain raw bytes")
    if isinstance(value, Sequence):
        return [_canonicalize(item) for item in value]
    raise ValueError(f"argument value is not canonically serializable: {type(value).__name__}")


class IdempotentInvoker:
    """基于执行账本的幂等调用器。

    同一 ``(operation, arguments)`` 请求只会真正执行一次：已提交的结果直接复用；
    发现同键的未完成记录（他人在跑或崩溃遗留）时抛出异常交由宿主决策，绝不自动
    重放外部副作用。

    Args:
        ledger: 保存执行记录的账本。
    """

    def __init__(self, ledger: ExecutionLedger) -> None:
        self._ledger = ledger

    async def invoke(
        self,
        operation: str,
        arguments: Mapping[str, object],
        executor: ExecutionCallable,
        *,
        run_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        tool_id: str | None = None,
    ) -> str:
        """幂等地执行一次外部副作用。

        Args:
            operation: 操作名称，参与幂等键计算。
            arguments: 请求参数，参与幂等键计算。
            executor: 真正执行副作用并返回结果字符串的异步回调。
            run_id: 所属运行标识。
            task_id: 可选任务标识。
            agent_id: 可选智能体标识。
            tool_id: 可选工具标识。

        Returns:
            本次执行的结果，或同键已提交记录缓存的结果。

        Raises:
            DuplicateExecutionError: 同键记录处于未完成或失败状态，需要宿主对账或决策。
            ExecutionLedgerError: 账本状态迁移非法。
            ValueError: 请求参数无法确定性序列化。
        """
        request_hash = canonical_request_hash(operation, arguments)
        candidate = ExecutionRecord(
            execution_id=uuid4().hex,
            run_id=run_id,
            request_hash=request_hash,
            task_id=task_id,
            agent_id=agent_id,
            tool_id=tool_id,
        )
        record = await self._ledger.prepare(candidate)
        if record.execution_id != candidate.execution_id:
            if record.status is ExecutionStatus.COMMITTED:
                if record.result_payload is None:
                    raise ExecutionLedgerError(
                        f"committed execution has no result payload: {record.execution_id}"
                    )
                return record.result_payload
            raise DuplicateExecutionError(record.execution_id, record.status)
        await self._ledger.mark_executing(record.execution_id)
        try:
            result = await executor()
        except Exception as exc:
            await self._ledger.fail(record.execution_id, f"{type(exc).__name__}: {exc}")
            raise
        await self._ledger.commit(record.execution_id, result)
        return result
