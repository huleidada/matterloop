"""Agent 间类型化消息与进程内邮箱。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from matterloop_agents.collaboration._immutability import freeze_mapping


class MessageType(str, Enum):
    """Agent 协作消息的稳定语义类型。"""

    TASK_ASSIGNMENT = "task.assignment"
    TASK_RESULT = "task.result"
    REQUEST = "request"
    RESPONSE = "response"
    INFORMATION = "information"
    FEEDBACK = "feedback"
    CONTROL = "control"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """在团队运行内从一个 Agent 发送给另一个 Agent 的不可变消息。

    Args:
        team_run_id: 消息所属团队运行标识。
        sender_agent_id: 发送方 Agent 标识。
        recipient_agent_id: 接收方 Agent 标识。
        message_type: 消息的稳定语义类型。
        content: 面向接收 Agent 的文本内容。
        correlation_id: 可选的请求、任务或上游消息关联标识。
        metadata: 不参与邮箱路由的只读扩展信息。
        message_id: 全局唯一消息标识。
        created_at: 带时区的消息创建时间。
    """

    team_run_id: str
    sender_agent_id: str
    recipient_agent_id: str
    message_type: MessageType
    content: str
    correlation_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """校验路由字段并冻结扩展信息。"""
        values = {
            "team_run_id": self.team_run_id,
            "sender_agent_id": self.sender_agent_id,
            "recipient_agent_id": self.recipient_agent_id,
            "message_id": self.message_id,
        }
        for name, value in values.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.correlation_id is not None and not self.correlation_id.strip():
            raise ValueError("correlation_id must not be empty when provided")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class InMemoryMailbox:
    """提供 FIFO、去重和原子批量领取语义的进程内邮箱。

    该实现适用于测试和单进程运行。``receive`` 会原子移除返回的消息；已经接收过的
    ``message_id`` 仍会保留在去重集合中，避免重试发送造成重复消费。
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[AgentMessage]] = {}
        self._known_message_ids: set[str] = set()
        self._lock = asyncio.Lock()

    async def send(self, message: AgentMessage) -> None:
        """把消息追加到接收方 FIFO 队列。

        Args:
            message: 等待投递的不可变消息。

        Raises:
            ValueError: 消息标识已经投递过。
        """
        async with self._lock:
            if message.message_id in self._known_message_ids:
                raise ValueError(f"message_id has already been sent: {message.message_id}")
            self._known_message_ids.add(message.message_id)
            self._queues.setdefault(message.recipient_agent_id, deque()).append(message)

    async def receive(
        self,
        recipient_agent_id: str,
        *,
        team_run_id: str | None = None,
        limit: int = 100,
    ) -> tuple[AgentMessage, ...]:
        """原子领取接收方的最早一批消息。

        Args:
            recipient_agent_id: 接收方 Agent 标识。
            team_run_id: 可选的团队运行过滤条件。
            limit: 本次最多领取的消息数量。

        Returns:
            按发送顺序排列并已从邮箱移除的消息。
        """
        self._validate_query(recipient_agent_id, team_run_id, limit)
        async with self._lock:
            selected = self._select(
                recipient_agent_id,
                team_run_id=team_run_id,
                limit=limit,
                remove=True,
            )
            return tuple(selected)

    async def peek(
        self,
        recipient_agent_id: str,
        *,
        team_run_id: str | None = None,
        limit: int = 100,
    ) -> tuple[AgentMessage, ...]:
        """读取但不移除接收方的最早一批消息。

        Args:
            recipient_agent_id: 接收方 Agent 标识。
            team_run_id: 可选的团队运行过滤条件。
            limit: 本次最多读取的消息数量。

        Returns:
            按发送顺序排列的消息。
        """
        self._validate_query(recipient_agent_id, team_run_id, limit)
        async with self._lock:
            return tuple(
                self._select(
                    recipient_agent_id,
                    team_run_id=team_run_id,
                    limit=limit,
                    remove=False,
                )
            )

    async def pending_count(
        self,
        recipient_agent_id: str,
        *,
        team_run_id: str | None = None,
    ) -> int:
        """返回接收方尚未领取的消息数。

        Args:
            recipient_agent_id: 接收方 Agent 标识。
            team_run_id: 可选的团队运行过滤条件。

        Returns:
            当前匹配的消息数量。
        """
        self._validate_query(recipient_agent_id, team_run_id, 1)
        async with self._lock:
            queue = self._queues.get(recipient_agent_id, ())
            return sum(
                1 for message in queue if team_run_id is None or message.team_run_id == team_run_id
            )

    def _select(
        self,
        recipient_agent_id: str,
        *,
        team_run_id: str | None,
        limit: int,
        remove: bool,
    ) -> list[AgentMessage]:
        queue = self._queues.get(recipient_agent_id)
        if not queue:
            return []
        selected: list[AgentMessage] = []
        retained: deque[AgentMessage] = deque()
        while queue:
            message = queue.popleft()
            matches = team_run_id is None or message.team_run_id == team_run_id
            if matches and len(selected) < limit:
                selected.append(message)
                if not remove:
                    retained.append(message)
            else:
                retained.append(message)
        if retained:
            self._queues[recipient_agent_id] = retained
        else:
            self._queues.pop(recipient_agent_id, None)
        return selected

    @staticmethod
    def _validate_query(
        recipient_agent_id: str,
        team_run_id: str | None,
        limit: int,
    ) -> None:
        if not recipient_agent_id.strip():
            raise ValueError("recipient_agent_id must not be empty")
        if team_run_id is not None and not team_run_id.strip():
            raise ValueError("team_run_id must not be empty when provided")
        if limit < 1:
            raise ValueError("limit must be at least 1")


__all__ = ["AgentMessage", "InMemoryMailbox", "MessageType"]
