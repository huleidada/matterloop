"""Working Memory 内存实现测试。"""

import asyncio

import pytest
from matterloop_memory import InMemoryWorkingMemory, PlanStepSummary, WorkingMemorySnapshot


def _snapshot(run_id: str = "run-1") -> WorkingMemorySnapshot:
    """构造一份带两步计划的测试快照。"""
    return WorkingMemorySnapshot(
        run_id=run_id,
        goal="计算聚合物玻璃化温度",
        plan=(
            PlanStepSummary("step-1", "构建模型", "completed"),
            PlanStepSummary("step-2", "运行模拟", "pending"),
        ),
        current_step_index=1,
        metadata={"owner": "matterloop"},
    )


def test_working_memory_saves_and_loads_snapshot() -> None:
    """保存后应能按 run_id 读回同一快照。"""

    async def scenario() -> None:
        memory = InMemoryWorkingMemory()
        snapshot = _snapshot()
        await memory.save_snapshot(snapshot)

        loaded = await memory.load_snapshot(snapshot.run_id)
        assert loaded is not None
        assert loaded.goal == snapshot.goal
        assert loaded.plan == snapshot.plan
        assert loaded.current_step_index == 1
        assert loaded.metadata["owner"] == "matterloop"
        assert await memory.load_snapshot("missing") is None

    asyncio.run(scenario())


def test_working_memory_records_step_results() -> None:
    """记录步骤结果后新快照与存储应同时更新。"""

    async def scenario() -> None:
        memory = InMemoryWorkingMemory()
        await memory.save_snapshot(_snapshot())

        updated = await memory.record_step_result("run-1", "step-1", "模型已构建")
        assert updated.step_results["step-1"] == "模型已构建"

        again = await memory.record_step_result("run-1", "step-2", "模拟完成")
        assert again.step_results == {"step-1": "模型已构建", "step-2": "模拟完成"}

        loaded = await memory.load_snapshot("run-1")
        assert loaded is not None
        assert dict(loaded.step_results) == {"step-1": "模型已构建", "step-2": "模拟完成"}

    asyncio.run(scenario())


def test_working_memory_rejects_unknown_run_and_empty_step() -> None:
    """未知运行或空步骤标识应抛出对应异常。"""

    async def scenario() -> None:
        memory = InMemoryWorkingMemory()
        with pytest.raises(KeyError):
            await memory.record_step_result("missing", "step-1", "结果")
        await memory.save_snapshot(_snapshot())
        with pytest.raises(ValueError):
            await memory.record_step_result("run-1", "  ", "结果")

    asyncio.run(scenario())


def test_working_memory_clear_reports_existence() -> None:
    """清理应返回快照是否存在，并真正删除数据。"""

    async def scenario() -> None:
        memory = InMemoryWorkingMemory()
        await memory.save_snapshot(_snapshot())

        assert await memory.clear("run-1")
        assert await memory.load_snapshot("run-1") is None
        assert not await memory.clear("run-1")

    asyncio.run(scenario())


def test_working_memory_snapshot_validates_fields() -> None:
    """快照必填字段与索引边界应被校验。"""
    with pytest.raises(ValueError):
        WorkingMemorySnapshot(run_id=" ", goal="目标")
    with pytest.raises(ValueError):
        WorkingMemorySnapshot(run_id="run-1", goal=" ")
    with pytest.raises(ValueError):
        WorkingMemorySnapshot(run_id="run-1", goal="目标", current_step_index=-1)
    with pytest.raises(ValueError):
        PlanStepSummary(" ", "描述", "pending")
