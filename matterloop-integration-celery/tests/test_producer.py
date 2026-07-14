"""Celery QueueProducer 消息边界与取消测试。"""

from __future__ import annotations

import asyncio
import json

from fakes import FakeCeleryApp
from matterloop_core import LoopRequest, ResumeMode
from matterloop_integration_celery import (
    RESUME_TASK_NAME,
    RUN_TASK_NAME,
    CeleryQueueBackend,
    resume_task_id,
    start_task_id,
)
from matterloop_runtime import QueueAction, QueueBackend, QueuedRun, QueueProducer


def test_producer_sends_only_start_and_resume_dtos() -> None:
    async def scenario() -> None:
        app = FakeCeleryApp()
        producer = CeleryQueueBackend(app, queue="loops")
        assert isinstance(producer, QueueProducer)
        # Celery 自己处理 broker 租约；推送适配器不伪装成主动拉取后端。
        assert not isinstance(producer, QueueBackend)

        await producer.enqueue(
            QueuedRun(
                run_id="run-1",
                action=QueueAction.START,
                request=LoopRequest(goal="目标", metadata={"trace": "t-1"}),
            )
        )
        await producer.enqueue(
            QueuedRun(
                run_id="run-1",
                action=QueueAction.RESUME,
                resume_mode=ResumeMode.REPLAN,
            )
        )

        start_name, start_kwargs, start_options = app.sent[0]
        assert start_name == RUN_TASK_NAME
        assert start_kwargs is not None
        assert set(start_kwargs) == {"run_id", "request"}
        assert start_options == {
            "task_id": start_task_id("run-1"),
            "serializer": "json",
            "queue": "loops",
        }
        json.dumps(start_kwargs, allow_nan=False)

        resume_name, resume_kwargs, resume_options = app.sent[1]
        assert resume_name == RESUME_TASK_NAME
        assert resume_kwargs == {"run_id": "run-1", "resume_mode": "replan"}
        assert resume_options["task_id"] == resume_task_id("run-1", "replan")

    asyncio.run(scenario())


def test_cancel_revokes_all_deterministic_task_ids_without_terminate() -> None:
    async def scenario() -> None:
        app = FakeCeleryApp()
        producer = CeleryQueueBackend(app)

        assert await producer.cancel("run-2")
        assert app.control.revoked == [
            (start_task_id("run-2"), False),
            (resume_task_id("run-2", "continue"), False),
            (resume_task_id("run-2", "replan"), False),
        ]

    asyncio.run(scenario())
