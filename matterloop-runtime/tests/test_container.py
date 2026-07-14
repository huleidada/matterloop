"""运行时组件容器热替换测试。"""

import asyncio

import pytest
from matterloop_runtime import RuntimeContainer


class Component:
    """暴露生命周期标记的测试组件。"""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.started = False
        self.closed = False
        self.fail_start = fail_start

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("start failed")
        self.started = True

    async def aclose(self) -> None:
        self.closed = True


async def test_replacement_waits_for_old_active_call_to_finish() -> None:
    old = Component()
    new = Component()
    container: RuntimeContainer[Component] = RuntimeContainer({"worker": old})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def use_old() -> None:
        async with container.acquire("worker") as component:
            assert component is old
            entered.set()
            await release.wait()

    task = asyncio.create_task(use_old())
    await entered.wait()
    await container.replace("worker", new)

    assert new.started
    assert container.get("worker") is new
    assert not old.closed
    release.set()
    await task
    assert old.closed
    await container.aclose()
    assert new.closed


async def test_failed_replacement_keeps_old_component() -> None:
    old = Component()
    failing = Component(fail_start=True)
    container: RuntimeContainer[Component] = RuntimeContainer({"worker": old})

    with pytest.raises(RuntimeError, match="start failed"):
        await container.replace("worker", failing)

    assert container.get("worker") is old
    assert not old.closed
