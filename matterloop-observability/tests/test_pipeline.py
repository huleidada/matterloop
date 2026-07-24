"""批量导出流水线测试。"""

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from matterloop_observability import BatchingPipeline, SpanRecord
from matterloop_observability.pipeline import ExportItem


def _span(name: str = "span") -> SpanRecord:
    """创建一条最小的跨度记录。"""
    moment = datetime.now(timezone.utc)
    return SpanRecord(
        trace_id="run-1",
        span_id=uuid4().hex,
        parent_span_id=None,
        name=name,
        observation_type="span",
        started_at=moment,
        ended_at=moment,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """在给定时限内轮询条件是否成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _CollectingExporter:
    """记录全部收到批次的导出器。"""

    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def export(self, batch: Sequence[ExportItem]) -> None:
        self.items.extend(batch)


def test_pipeline_flushes_at_batch_threshold() -> None:
    """达到批量阈值时应立即导出，而不是等待时间间隔。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=3, flush_interval=30.0)
    try:
        for index in range(3):
            pipeline.enqueue(_span(f"span-{index}"))

        assert _wait_until(lambda: len(exporter.items) == 3, timeout=2.0)
    finally:
        pipeline.shutdown()


def test_pipeline_shutdown_wakes_an_idle_worker_immediately() -> None:
    """关闭空闲流水线不应等待完整 flush_interval。"""
    pipeline = BatchingPipeline(_CollectingExporter(), flush_interval=30.0)

    started_at = time.monotonic()
    pipeline.shutdown()

    assert time.monotonic() - started_at < 1.0


def test_pipeline_drops_new_items_when_queue_is_full() -> None:
    """队列满时新条目应被丢弃并计数，绝不抛出。"""
    entered = threading.Event()
    release = threading.Event()

    class _BlockingExporter:
        def export(self, batch: Sequence[ExportItem]) -> None:
            entered.set()
            release.wait(timeout=5.0)

    pipeline = BatchingPipeline(
        _BlockingExporter(),
        flush_at=1,
        flush_interval=0.05,
        max_queue_size=1,
    )
    try:
        pipeline.enqueue(_span("blocked"))
        assert entered.wait(timeout=2.0)
        pipeline.enqueue(_span("queued"))
        pipeline.enqueue(_span("dropped"))

        assert pipeline.dropped_count == 1
    finally:
        release.set()
        pipeline.shutdown()


def test_pipeline_retries_failed_export_once(caplog: pytest.LogCaptureFixture) -> None:
    """导出异常应重试一次后丢弃，且不会传播到调用方。"""

    class _FailingExporter:
        def __init__(self) -> None:
            self.calls = 0

        def export(self, batch: Sequence[ExportItem]) -> None:
            self.calls += 1
            raise RuntimeError("backend unavailable")

    exporter = _FailingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=0.05)
    try:
        pipeline.enqueue(_span())
        pipeline.flush()

        assert exporter.calls == 2
        assert "丢弃一批记录" in caplog.text
    finally:
        pipeline.shutdown()


def test_pipeline_flush_and_shutdown_deliver_everything() -> None:
    """flush 应阻塞至队列清空，shutdown 可重复且之后入队被丢弃。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=50, flush_interval=0.05)

    for index in range(10):
        pipeline.enqueue(_span(f"span-{index}"))
    pipeline.flush()
    assert len(exporter.items) == 10

    pipeline.shutdown()
    pipeline.shutdown()
    pipeline.enqueue(_span("late"))
    assert pipeline.dropped_count == 1
    assert len(exporter.items) == 10


def test_pipeline_serializes_enqueue_with_shutdown() -> None:
    """关闭与入队交错时，已开始的入队必须排空而不能遗留在无 worker 队列中。"""
    exporter = _CollectingExporter()
    pipeline = BatchingPipeline(exporter, flush_at=1, flush_interval=30.0)
    entered_put = threading.Event()
    allow_put = threading.Event()
    original_put = pipeline._queue.put_nowait

    def delayed_put(item: object) -> None:
        entered_put.set()
        assert allow_put.wait(timeout=2.0)
        original_put(item)

    pipeline._queue.put_nowait = delayed_put  # type: ignore[method-assign]
    producer = threading.Thread(target=lambda: pipeline.enqueue(_span("racing")))
    producer.start()
    assert entered_put.wait(timeout=2.0)
    closer = threading.Thread(target=pipeline.shutdown)
    closer.start()
    allow_put.set()
    producer.join(timeout=2.0)
    closer.join(timeout=2.0)

    assert not producer.is_alive()
    assert not closer.is_alive()
    assert [item.name for item in exporter.items] == ["racing"]


def test_pipeline_rejects_invalid_configuration() -> None:
    """非法的批量与容量配置应在构造时拒绝。"""
    exporter = _CollectingExporter()
    with pytest.raises(ValueError, match="flush_at"):
        BatchingPipeline(exporter, flush_at=0)
    with pytest.raises(ValueError, match="max_queue_size"):
        BatchingPipeline(exporter, max_queue_size=0)
