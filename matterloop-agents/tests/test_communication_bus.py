"""Agent 消息总线的请求响应、超时、背压、事件与广播测试。"""

from __future__ import annotations

import asyncio

import pytest
from matterloop_agents.communication.bus import (
    AgentEventMessage,
    AgentMessageBus,
    AgentRequest,
    AgentResponse,
    BroadcastMessage,
    MessageBusBackpressureError,
    RequestTimeoutError,
    UnknownCorrelationError,
    UnknownRecipientError,
)


def _bus(*agent_ids: str, queue_maxsize: int = 256) -> AgentMessageBus:
    """创建并预注册若干 Agent 的总线测试辅助函数。"""
    bus = AgentMessageBus(queue_maxsize=queue_maxsize)
    for agent_id in agent_ids:
        bus.register(agent_id)
    return bus


async def test_request_response_round_trip() -> None:
    bus = _bus("alice", "bob")

    async def responder() -> None:
        message = await bus.receive("bob", timeout=1.0)
        assert isinstance(message, AgentRequest)
        assert message.action == "compute"
        assert message.payload["value"] == 21
        await bus.respond(
            AgentResponse(
                sender="bob",
                recipient=message.sender,
                correlation_id=message.correlation_id,
                payload={"result": 42},
            )
        )

    responder_task = asyncio.create_task(responder())
    request = AgentRequest(sender="alice", recipient="bob", action="compute", payload={"value": 21})
    response = await bus.send_request(request)
    await responder_task
    assert response.correlation_id == request.correlation_id
    assert response.payload["result"] == 42
    assert response.error == ""


async def test_send_request_times_out_without_response() -> None:
    bus = _bus("alice", "bob")
    request = AgentRequest(
        sender="alice",
        recipient="bob",
        action="compute",
        reply_timeout_seconds=0.05,
    )
    with pytest.raises(RequestTimeoutError):
        await bus.send_request(request)


async def test_send_request_to_unknown_recipient_raises() -> None:
    bus = _bus("alice")
    request = AgentRequest(sender="alice", recipient="ghost", action="ping")
    with pytest.raises(UnknownRecipientError):
        await bus.send_request(request)


async def test_respond_without_pending_request_raises() -> None:
    bus = _bus("alice", "bob")
    response = AgentResponse(sender="bob", recipient="alice", correlation_id="missing")
    with pytest.raises(UnknownCorrelationError):
        await bus.respond(response)


async def test_backpressure_when_inbox_is_full() -> None:
    bus = _bus("alice", "bob", queue_maxsize=1)
    await bus.broadcast(BroadcastMessage(sender="alice"))
    with pytest.raises(MessageBusBackpressureError):
        await bus.broadcast(BroadcastMessage(sender="alice"))


async def test_backpressure_on_full_request_inbox() -> None:
    bus = _bus("alice", "bob", queue_maxsize=1)
    await bus.broadcast(BroadcastMessage(sender="alice"))
    request = AgentRequest(sender="alice", recipient="bob", action="ping")
    with pytest.raises(MessageBusBackpressureError):
        await bus.send_request(request)


async def test_broadcast_excludes_sender() -> None:
    bus = _bus("alice", "bob", "carol")
    message = BroadcastMessage(sender="alice", payload={"note": "hello"})
    await bus.broadcast(message)
    assert (await bus.receive("bob", timeout=1.0)).message_id == message.message_id
    assert (await bus.receive("carol", timeout=1.0)).message_id == message.message_id
    with pytest.raises(RequestTimeoutError):
        await bus.receive("alice", timeout=0.05)


async def test_publish_event_reaches_only_topic_subscribers() -> None:
    bus = _bus("alice", "bob", "carol")
    bus.subscribe_topic("bob", "materials.updated")
    bus.subscribe_topic("carol", "jobs.finished")
    event = AgentEventMessage(sender="alice", topic="materials.updated", payload={"id": "m-1"})
    await bus.publish_event(event)
    received = await bus.receive("bob", timeout=1.0)
    assert isinstance(received, AgentEventMessage)
    assert received.topic == "materials.updated"
    with pytest.raises(RequestTimeoutError):
        await bus.receive("carol", timeout=0.05)


async def test_publish_event_without_subscribers_is_noop() -> None:
    bus = _bus("alice")
    await bus.publish_event(AgentEventMessage(sender="alice", topic="unwatched"))


async def test_unsubscribe_topic_stops_delivery() -> None:
    bus = _bus("alice", "bob")
    bus.subscribe_topic("bob", "news")
    bus.unsubscribe_topic("bob", "news")
    await bus.publish_event(AgentEventMessage(sender="alice", topic="news"))
    with pytest.raises(RequestTimeoutError):
        await bus.receive("bob", timeout=0.05)


async def test_unregister_removes_queue_and_subscriptions() -> None:
    bus = _bus("alice", "bob")
    bus.subscribe_topic("bob", "news")
    bus.unregister("bob")
    await bus.publish_event(AgentEventMessage(sender="alice", topic="news"))
    with pytest.raises(UnknownRecipientError):
        await bus.receive("bob", timeout=0.05)


async def test_register_duplicate_agent_raises() -> None:
    bus = _bus("alice")
    with pytest.raises(ValueError, match="already registered"):
        bus.register("alice")


async def test_receive_for_unknown_agent_raises() -> None:
    bus = _bus()
    with pytest.raises(UnknownRecipientError):
        await bus.receive("ghost", timeout=0.05)


async def test_messages_are_received_in_fifo_order() -> None:
    bus = _bus("alice", "bob")
    first = BroadcastMessage(sender="alice", payload={"seq": 1})
    second = BroadcastMessage(sender="alice", payload={"seq": 2})
    await bus.broadcast(first)
    await bus.broadcast(second)
    assert (await bus.receive("bob", timeout=1.0)).message_id == first.message_id
    assert (await bus.receive("bob", timeout=1.0)).message_id == second.message_id


def test_request_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="reply_timeout_seconds"):
        AgentRequest(sender="a", recipient="b", action="x", reply_timeout_seconds=0)


def test_message_payloads_are_frozen() -> None:
    request = AgentRequest(sender="a", recipient="b", action="x", payload={"k": [1, 2]})
    assert request.payload["k"] == (1, 2)
    with pytest.raises(TypeError):
        request.payload["k"] = "changed"  # type: ignore[index]
