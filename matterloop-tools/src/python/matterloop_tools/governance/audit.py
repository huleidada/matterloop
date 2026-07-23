"""MCP 工具调用审计记录与落地。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from matterloop_tools.governance.access import Principal


def stable_digest(value: object) -> str:
    """计算 JSON 兼容值的确定性 sha256 摘要。

    对象键按排序后编码，因此键顺序不同的等值参数产生相同摘要；审计记录
    默认只保存摘要而非参数原文，避免敏感内容落盘。

    Args:
        value: 需要摘要的 JSON 兼容值。

    Returns:
        十六进制 sha256 摘要字符串。
    """
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_encode_fallback,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _encode_fallback(value: object) -> object:
    """将非原生 JSON 类型规约为可确定编码的形式。"""
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一次工具调用的完整审计记录。

    Args:
        record_id: 每次调用唯一的记录标识。
        principal: 发起调用的主体身份。
        tool_name: 工具注册名称。
        arguments_digest: 参数的确定性摘要，默认不保存原文。
        decision: 治理决策结果，例如 ``allowed`` / ``denied`` /
            ``quota_exceeded`` / ``error``。
        started_at: 调用开始的 Unix 时间戳（秒）。
        finished_at: 记录生成的 Unix 时间戳（秒）。
        result_digest: 成功结果的确定性摘要；未执行或失败时为空。
        arguments_snapshot: 显式开启原文留存时的参数快照。
        error: 拒绝原因或执行异常信息，默认空。
    """

    record_id: str
    principal: Principal
    tool_name: str
    arguments_digest: str
    decision: str
    started_at: float
    finished_at: float
    result_digest: str | None = None
    arguments_snapshot: Mapping[str, object] | None = None
    error: str = ""

    def __post_init__(self) -> None:
        """校验关键字段并冻结可选参数快照。"""
        if not self.record_id.strip():
            raise ValueError("record_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("tool_name must not be empty")
        if not self.decision.strip():
            raise ValueError("decision must not be empty")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.arguments_snapshot is not None:
            object.__setattr__(
                self,
                "arguments_snapshot",
                MappingProxyType(dict(self.arguments_snapshot)),
            )


@runtime_checkable
class AuditSink(Protocol):
    """审计记录落地协议。"""

    async def record(self, record: AuditRecord) -> None:
        """持久化一条审计记录。"""
        ...


class InMemoryAuditSink:
    """保存在进程内存中的审计落地实现，主要用于测试与本地开发。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = []

    async def record(self, record: AuditRecord) -> None:
        """追加一条审计记录。

        Args:
            record: 需要保存的记录。
        """
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[AuditRecord, ...]:
        """返回按写入顺序排列的全部记录快照。"""
        with self._lock:
            return tuple(self._records)

    def records_for_tool(self, tool_name: str) -> tuple[AuditRecord, ...]:
        """按工具名称查询审计记录。

        Args:
            tool_name: 工具注册名称。

        Returns:
            该工具的全部记录，按写入顺序排列。
        """
        with self._lock:
            return tuple(record for record in self._records if record.tool_name == tool_name)

    def records_for_principal(self, principal: Principal) -> tuple[AuditRecord, ...]:
        """按主体身份查询审计记录。

        Args:
            principal: 发起调用的主体身份。

        Returns:
            该主体的全部记录，按写入顺序排列。
        """
        with self._lock:
            return tuple(record for record in self._records if record.principal == principal)
