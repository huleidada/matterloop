"""脱敏和组合事件发布器测试。"""

import asyncio
import json
import logging

import pytest
from matterloop_core import LoopContext, LoopEvent, LoopEventType, LoopRequest
from matterloop_observability import (
    CompositeEventPublisher,
    HandlerEventPublisher,
    PublisherFailureMode,
    Redactor,
    StructuredLoggingHandler,
)


def test_redactor_filters_nested_sensitive_fields() -> None:
    """默认敏感字段在嵌套结构中也应被过滤。"""
    redactor = Redactor()

    result = redactor.redact({"authorization": "secret", "nested": {"token": "value"}})

    assert result == {
        "authorization": "[REDACTED]",
        "nested": {"token": "[REDACTED]"},
    }


def test_redactor_filters_common_sensitive_field_variants() -> None:
    """带命名空间或前缀的凭据字段同样不能泄漏。"""
    redactor = Redactor(extra_fields=("tenant_credential",))

    result = redactor.redact(
        {
            "access_token": "secret-1",
            "set-cookie": "secret-2",
            "openai.api_key": "secret-3",
            "tenant_credential": "secret-4",
            "safe": "visible",
        }
    )

    assert result == {
        "access_token": "[REDACTED]",
        "set-cookie": "[REDACTED]",
        "openai.api_key": "[REDACTED]",
        "tenant_credential": "[REDACTED]",
        "safe": "visible",
    }


def test_asyncio_is_available_for_async_publishers() -> None:
    """测试环境应能运行异步可观测性组件。"""

    async def scenario() -> bool:
        await asyncio.sleep(0)
        return True

    assert asyncio.run(scenario())


def _event() -> LoopEvent:
    """创建不依赖运行时的测试事件。"""
    context = LoopContext(
        LoopRequest(
            "验证可观测性",
            metadata={"access_token": "must-not-leak", "tenant": "demo"},
        )
    )
    return LoopEvent(LoopEventType.LOOP_STARTED, context)


def test_composite_publisher_logs_and_continues(caplog: pytest.LogCaptureFixture) -> None:
    """容错模式不能阻断后续审计发布器。"""
    received: list[str] = []

    async def fail(event: LoopEvent) -> None:
        del event
        raise RuntimeError("publisher failed")

    async def collect(event: LoopEvent) -> None:
        received.append(event.event_type.value)

    publisher = CompositeEventPublisher(
        (HandlerEventPublisher(fail), HandlerEventPublisher(collect)),
        PublisherFailureMode.LOG_AND_CONTINUE,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(publisher.publish(_event()))

    assert received == [LoopEventType.LOOP_STARTED.value]
    assert "事件发布器执行失败" in caplog.text


def test_composite_publisher_raise_mode_propagates() -> None:
    """严格审计模式必须把发布失败传播给调用方。"""

    async def fail(event: LoopEvent) -> None:
        del event
        raise RuntimeError("publisher failed")

    publisher = CompositeEventPublisher(
        (HandlerEventPublisher(fail),),
        PublisherFailureMode.RAISE,
    )

    with pytest.raises(RuntimeError, match="publisher failed"):
        asyncio.run(publisher.publish(_event()))


def test_structured_logging_redacts_request_metadata(caplog: pytest.LogCaptureFixture) -> None:
    """结构化日志不能包含上下文中的凭据值。"""
    logger = logging.getLogger("matterloop.test.events")
    handler = StructuredLoggingHandler(logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        handler(_event())

    payload = json.loads(caplog.records[-1].message)
    assert payload["metadata"] == {
        "access_token": "[REDACTED]",
        "tenant": "demo",
    }
    assert "must-not-leak" not in caplog.text
