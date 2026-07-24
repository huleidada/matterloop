"""跨度与评分的有界批量导出流水线。"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Union, cast

if TYPE_CHECKING:
    from matterloop_observability.exporter import SpanExporter
    from matterloop_observability.scores import Score
    from matterloop_observability.spans import SpanRecord

logger = logging.getLogger(__name__)

ExportItem = Union["SpanRecord", "Score"]
"""流水线承载的导出条目类型。"""


class BatchingPipeline:
    """在后台守护线程中聚批并把观测记录交给导出器。

    队列有界且满时丢弃新条目，导出失败重试一次后丢弃；任何情况下都不会把异常
    传播到调用方线程，保证观测路径永远不会阻断主流程。
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        flush_at: int = 50,
        flush_interval: float = 5.0,
        max_queue_size: int = 10000,
    ) -> None:
        if flush_at < 1:
            raise ValueError("flush_at must be at least 1")
        if flush_interval <= 0:
            raise ValueError("flush_interval must be greater than 0")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self._exporter = exporter
        self._flush_at = flush_at
        self._flush_interval = flush_interval
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_queue_size)
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._closed = False
        self._stop_sentinel = object()
        self._dropped = 0
        self._worker = threading.Thread(
            target=self._run,
            name="matterloop-observability-pipeline",
            daemon=True,
        )
        self._worker.start()

    @property
    def dropped_count(self) -> int:
        """返回因队列满而被丢弃的条目数量。"""
        with self._state_lock:
            return self._dropped

    def enqueue(self, item: ExportItem) -> None:
        """把一条观测记录放入队列；队列满或已关闭时丢弃并告警，绝不抛出。"""
        with self._state_lock:
            if self._closed:
                self._dropped += 1
                logger.warning("观测导出流水线已关闭，丢弃一条记录")
                return
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped += 1
                logger.warning("观测导出队列已满，丢弃一条记录", extra={"dropped": self._dropped})
            except Exception:
                logger.exception("观测记录入队失败")

    def flush(self) -> None:
        """阻塞直到已入队的记录全部被导出或丢弃。"""
        self._queue.join()

    def shutdown(self) -> None:
        """排空队列后停止后台线程，可重复调用。"""
        with self._state_lock:
            if not self._closed:
                self._closed = True
                self._stop.set()
                with suppress(queue.Full):
                    # 唤醒空闲 worker；队列满时 worker 已可立即消费现有条目。
                    self._queue.put_nowait(self._stop_sentinel)
        self._worker.join(timeout=self._flush_interval + 5.0)
        self.flush()

    def _run(self) -> None:
        """按批量阈值或时间间隔聚批导出。"""
        batch: list[ExportItem] = []
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                if batch:
                    self._export(batch)
                    batch = []
                if self._stop.is_set() and self._queue.empty():
                    break
                continue
            if item is self._stop_sentinel:
                self._queue.task_done()
                if batch:
                    self._export(batch)
                    batch = []
                if self._stop.is_set() and self._queue.empty():
                    break
                continue
            batch.append(cast(ExportItem, item))
            saw_stop_sentinel = False
            while len(batch) < self._flush_at:
                try:
                    next_item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if next_item is self._stop_sentinel:
                    self._queue.task_done()
                    saw_stop_sentinel = True
                    break
                batch.append(cast(ExportItem, next_item))
            if len(batch) >= self._flush_at or saw_stop_sentinel or self._stop.is_set():
                self._export(batch)
                batch = []
            if self._stop.is_set() and self._queue.empty():
                break
        if batch:
            self._export(batch)

    def _export(self, batch: Sequence[ExportItem]) -> None:
        """导出一批记录，失败重试一次后丢弃并告警。"""
        for attempt in range(2):
            try:
                self._exporter.export(batch)
                break
            except Exception:
                if attempt == 1:
                    logger.warning("观测记录导出失败，丢弃一批记录", exc_info=True)
        for _ in batch:
            self._queue.task_done()

    def __enter__(self) -> BatchingPipeline:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()
