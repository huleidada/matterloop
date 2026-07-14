"""实现 Celery 推送式 QueueProducer 适配器。"""

from __future__ import annotations

import asyncio

from matterloop_runtime import QueueAction, QueuedRun

from matterloop_integration_celery.codec import CeleryMessageCodec
from matterloop_integration_celery.protocols import CeleryApp

RUN_TASK_NAME = "matterloop.run"
RESUME_TASK_NAME = "matterloop.resume"


def start_task_id(run_id: str) -> str:
    """为启动命令生成可重复计算的 Celery 任务标识。"""
    return f"matterloop:start:{run_id}"


def resume_task_id(run_id: str, mode: str) -> str:
    """为指定恢复模式生成可重复计算的 Celery 任务标识。"""
    return f"matterloop:resume:{mode}:{run_id}"


class CeleryQueueProducer:
    """把 `QueuedRun` 推送为只含 DTO 的 Celery 任务。

    Args:
        app: Celery 应用或满足最小协议的兼容对象。
        queue: 可选目标队列名称。
        codec: 可选消息编解码器。
    """

    def __init__(
        self,
        app: CeleryApp,
        *,
        queue: str | None = None,
        codec: CeleryMessageCodec | None = None,
    ) -> None:
        if queue is not None and not queue.strip():
            raise ValueError("queue must not be empty")
        self._app = app
        self._queue = queue
        self._codec = codec or CeleryMessageCodec()

    async def enqueue(self, job: QueuedRun) -> None:
        """提交启动或恢复命令，不序列化任何运行时实例。

        Args:
            job: Runtime 创建的最小队列命令。
        """
        if job.action is QueueAction.START:
            if job.request is None:
                raise ValueError("START command requires a request")
            task_name = RUN_TASK_NAME
            task_id = start_task_id(job.run_id)
            payload: dict[str, object] = {
                "run_id": job.run_id,
                "request": self._codec.encode_request(job.request),
            }
        else:
            task_name = RESUME_TASK_NAME
            task_id = resume_task_id(job.run_id, job.resume_mode.value)
            payload = {
                "run_id": job.run_id,
                "resume_mode": job.resume_mode.value,
            }
        await asyncio.to_thread(self._send_task, task_name, payload, task_id)

    async def cancel(self, run_id: str) -> bool:
        """尽力撤销该运行所有确定性 Celery 任务标识。

        Celery 的非终止撤销只保证尽力阻止尚未执行的任务。运行状态由上层 `QueueRuntime`
        和共享仓储负责更新。

        Args:
            run_id: 需要取消的运行标识。

        Returns:
            成功提交撤销请求时返回 `True`。
        """
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        identifiers = (
            start_task_id(run_id),
            resume_task_id(run_id, "continue"),
            resume_task_id(run_id, "replan"),
        )
        for task_id in identifiers:
            await asyncio.to_thread(self._app.control.revoke, task_id, terminate=False)
        return True

    def _send_task(
        self,
        task_name: str,
        payload: dict[str, object],
        task_id: str,
    ) -> None:
        if self._queue is None:
            self._app.send_task(
                task_name,
                kwargs=payload,
                task_id=task_id,
                serializer="json",
            )
            return
        self._app.send_task(
            task_name,
            kwargs=payload,
            task_id=task_id,
            serializer="json",
            queue=self._queue,
        )


class CeleryQueueBackend(CeleryQueueProducer):
    """Celery 推送式队列适配器的兼容名称。

    该类型满足 `QueueProducer`。Celery Worker 自身拥有 broker 消息租约，因此本类型刻意
    不实现主动拉取式 `QueueBackend` 的 `lease/acknowledge/release`，避免制造虚假语义。
    """
