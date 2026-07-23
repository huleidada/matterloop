"""进程内异步 Agent 消息总线：请求响应、事件订阅与广播。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from matterloop_agents.communication._immutability import freeze_mapping
from matterloop_agents.communication.contract import CommunicationError

DEFAULT_QUEUE_MAXSIZE = 256


class MessageBusError(CommunicationError):
    """消息总线所有异常的基类。"""


class UnknownRecipientError(MessageBusError):
    """收件人没有在总线上注册。"""


class RequestTimeoutError(MessageBusError):
    """在超时时间内没有等到匹配的响应或消息。"""


class MessageBusBackpressureError(MessageBusError):
    """收件队列已满，投递被背压拒绝。"""


class UnknownCorrelationError(MessageBusError):
    """响应的关联标识没有对应的等待中请求。"""


def _new_message_id() -> str:
    """生成全局唯一消息标识。"""
    return uuid4().hex


def _now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """要求收件 Agent 执行动作并回复的请求消息。

    Args:
        sender: 发送方 Agent 标识。
        recipient: 收件方 Agent 标识。
        action: 请求收件方执行的动作名。
        payload: 只读请求参数。
        correlation_id: 用于匹配响应的关联标识。
        reply_timeout_seconds: 等待响应的超时秒数。
        message_id: 全局唯一消息标识。
        created_at: 带时区的消息创建时间。
    """

    sender: str
    recipient: str
    action: str
    payload: Mapping[str, object] = field(default_factory=dict)
    correlation_id: str = field(default_factory=_new_message_id)
    reply_timeout_seconds: float = 30.0
    message_id: str = field(default_factory=_new_message_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        """校验路由字段与超时配置并冻结载荷。"""
        _require_non_empty(sender=self.sender, recipient=self.recipient, action=self.action)
        _require_non_empty(correlation_id=self.correlation_id, message_id=self.message_id)
        if self.reply_timeout_seconds <= 0:
            raise ValueError("reply_timeout_seconds must be positive")
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """对某个请求的响应消息。

    Args:
        sender: 发送方 Agent 标识。
        recipient: 原请求发送方，即响应的收件方。
        correlation_id: 与原请求一致的关联标识。
        payload: 只读响应数据。
        error: 处理失败时的错误描述；成功时为空字符串。
        message_id: 全局唯一消息标识。
        created_at: 带时区的消息创建时间。
    """

    sender: str
    recipient: str
    correlation_id: str
    payload: Mapping[str, object] = field(default_factory=dict)
    error: str = ""
    message_id: str = field(default_factory=_new_message_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        """校验路由字段并冻结载荷。"""
        _require_non_empty(
            sender=self.sender,
            recipient=self.recipient,
            correlation_id=self.correlation_id,
            message_id=self.message_id,
        )
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class AgentEventMessage:
    """发布到某个主题、投递给全部订阅者的事件消息。

    Args:
        sender: 发送方 Agent 标识。
        topic: 事件所属主题。
        payload: 只读事件数据。
        message_id: 全局唯一消息标识。
        created_at: 带时区的消息创建时间。
    """

    sender: str
    topic: str
    payload: Mapping[str, object] = field(default_factory=dict)
    message_id: str = field(default_factory=_new_message_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        """校验路由字段并冻结载荷。"""
        _require_non_empty(sender=self.sender, topic=self.topic, message_id=self.message_id)
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class BroadcastMessage:
    """投递给发送者以外全部注册 Agent 的广播消息。

    Args:
        sender: 发送方 Agent 标识。
        payload: 只读广播数据。
        topic: 可选的语义主题标注；广播不按主题过滤。
        message_id: 全局唯一消息标识。
        created_at: 带时区的消息创建时间。
    """

    sender: str
    payload: Mapping[str, object] = field(default_factory=dict)
    topic: str | None = None
    message_id: str = field(default_factory=_new_message_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        """校验路由字段并冻结载荷。"""
        _require_non_empty(sender=self.sender, message_id=self.message_id)
        if self.topic is not None and not self.topic.strip():
            raise ValueError("topic must not be empty when provided")
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


BusMessage = AgentRequest | AgentResponse | AgentEventMessage | BroadcastMessage


def _require_non_empty(**values: str) -> None:
    """校验一组字符串字段非空。"""
    for name, value in values.items():
        if not value.strip():
            raise ValueError(f"{name} must not be empty")


class AgentMessageBus:
    """单事件循环内的进程内 Agent 消息总线。

    每个注册的 Agent 拥有一个有界收件队列；队列满时投递抛出背压异常而不是阻塞。
    请求响应通过 ``correlation_id`` 与 ``asyncio.Future`` 匹配，事件按主题订阅
    分发，广播投递给发送者以外的全部注册 Agent。
    """

    def __init__(self, *, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        """初始化总线。

        Args:
            queue_maxsize: 每个 Agent 收件队列的容量上限。

        Raises:
            ValueError: 队列容量不是正数。
        """
        if queue_maxsize < 1:
            raise ValueError("queue_maxsize must be at least 1")
        self._queue_maxsize = queue_maxsize
        self._queues: dict[str, asyncio.Queue[BusMessage]] = {}
        self._topic_subscribers: dict[str, set[str]] = {}
        self._pending_replies: dict[str, asyncio.Future[AgentResponse]] = {}

    def register(self, agent_id: str) -> None:
        """为 Agent 创建有界收件队列。

        Args:
            agent_id: 待注册的 Agent 标识。

        Raises:
            ValueError: 标识为空或已经注册。
        """
        if not agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if agent_id in self._queues:
            raise ValueError(f"agent is already registered on the bus: {agent_id}")
        self._queues[agent_id] = asyncio.Queue(maxsize=self._queue_maxsize)

    def unregister(self, agent_id: str) -> None:
        """注销 Agent 并清除其全部主题订阅。

        Args:
            agent_id: 待注销的 Agent 标识。

        Raises:
            UnknownRecipientError: Agent 没有注册。
        """
        if agent_id not in self._queues:
            raise UnknownRecipientError(f"agent is not registered on the bus: {agent_id}")
        del self._queues[agent_id]
        for subscribers in self._topic_subscribers.values():
            subscribers.discard(agent_id)

    def subscribe_topic(self, agent_id: str, topic: str) -> None:
        """让已注册 Agent 订阅一个事件主题。

        Args:
            agent_id: 订阅方 Agent 标识。
            topic: 订阅的主题名。

        Raises:
            UnknownRecipientError: Agent 没有注册。
            ValueError: 主题名为空。
        """
        if agent_id not in self._queues:
            raise UnknownRecipientError(f"agent is not registered on the bus: {agent_id}")
        if not topic.strip():
            raise ValueError("topic must not be empty")
        self._topic_subscribers.setdefault(topic, set()).add(agent_id)

    def unsubscribe_topic(self, agent_id: str, topic: str) -> None:
        """取消 Agent 对某个主题的订阅；未订阅时为空操作。

        Args:
            agent_id: 订阅方 Agent 标识。
            topic: 取消订阅的主题名。
        """
        subscribers = self._topic_subscribers.get(topic)
        if subscribers is not None:
            subscribers.discard(agent_id)
            if not subscribers:
                del self._topic_subscribers[topic]

    async def send_request(self, request: AgentRequest) -> AgentResponse:
        """投递请求并等待关联标识匹配的响应。

        Args:
            request: 待投递的请求消息。

        Returns:
            收件方通过 :meth:`respond` 提交的响应。

        Raises:
            UnknownRecipientError: 收件方没有注册。
            MessageBusBackpressureError: 收件队列已满。
            RequestTimeoutError: 在 ``reply_timeout_seconds`` 内没有等到响应。
            ValueError: 同一关联标识已有等待中的请求。
        """
        if request.correlation_id in self._pending_replies:
            raise ValueError(
                f"correlation_id already has a pending request: {request.correlation_id}"
            )
        future: asyncio.Future[AgentResponse] = asyncio.get_running_loop().create_future()
        self._pending_replies[request.correlation_id] = future
        try:
            self._deliver(request.recipient, request)
            return await asyncio.wait_for(future, timeout=request.reply_timeout_seconds)
        except asyncio.TimeoutError:
            raise RequestTimeoutError(
                f"request timed out after {request.reply_timeout_seconds}s:"
                f" {request.sender} -> {request.recipient} ({request.action})"
            ) from None
        finally:
            self._pending_replies.pop(request.correlation_id, None)

    async def respond(self, response: AgentResponse) -> None:
        """用响应完成对应请求的等待 Future。

        Args:
            response: 关联标识与某个等待中请求一致的响应。

        Raises:
            UnknownCorrelationError: 没有等待该关联标识的请求（可能已超时）。
        """
        future = self._pending_replies.get(response.correlation_id)
        if future is None or future.done():
            raise UnknownCorrelationError(
                f"no pending request for correlation_id: {response.correlation_id}"
            )
        future.set_result(response)

    async def publish_event(self, event: AgentEventMessage) -> None:
        """把事件投递给订阅了对应主题的全部 Agent。

        没有任何订阅者时是空操作。已注销但残留在订阅表中的 Agent 会被跳过。

        Args:
            event: 待发布的事件消息。

        Raises:
            MessageBusBackpressureError: 某个订阅者的收件队列已满。
        """
        for agent_id in sorted(self._topic_subscribers.get(event.topic, ())):
            if agent_id in self._queues:
                self._deliver(agent_id, event)

    async def broadcast(self, message: BroadcastMessage) -> None:
        """把广播投递给发送者以外的全部注册 Agent。

        Args:
            message: 待广播的消息。

        Raises:
            MessageBusBackpressureError: 某个 Agent 的收件队列已满。
        """
        for agent_id in sorted(self._queues):
            if agent_id != message.sender:
                self._deliver(agent_id, message)

    async def receive(self, agent_id: str, timeout: float | None = None) -> BusMessage:
        """从 Agent 的收件队列取出下一条消息。

        Args:
            agent_id: 收件方 Agent 标识。
            timeout: 可选的等待超时秒数；``None`` 表示一直等待。

        Returns:
            按投递顺序排列的下一条消息。

        Raises:
            UnknownRecipientError: Agent 没有注册。
            RequestTimeoutError: 在超时时间内没有收到消息。
        """
        queue = self._queues.get(agent_id)
        if queue is None:
            raise UnknownRecipientError(f"agent is not registered on the bus: {agent_id}")
        if timeout is None:
            return await queue.get()
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RequestTimeoutError(
                f"no message received within {timeout}s for agent: {agent_id}"
            ) from None

    def _deliver(self, recipient: str, message: BusMessage) -> None:
        """非阻塞投递消息到收件队列。"""
        queue = self._queues.get(recipient)
        if queue is None:
            raise UnknownRecipientError(f"agent is not registered on the bus: {recipient}")
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            raise MessageBusBackpressureError(
                f"inbox queue is full for agent: {recipient}"
            ) from None


__all__ = [
    "AgentEventMessage",
    "AgentMessageBus",
    "AgentRequest",
    "AgentResponse",
    "BroadcastMessage",
    "BusMessage",
    "MessageBusBackpressureError",
    "MessageBusError",
    "RequestTimeoutError",
    "UnknownCorrelationError",
    "UnknownRecipientError",
]
