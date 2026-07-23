"""Procedural Memory 内存实现测试。"""

import asyncio

import pytest
from matterloop_memory import (
    BestPractice,
    InMemoryProceduralMemory,
    SkillEntry,
    ToolUsageStat,
    WorkflowTemplate,
)


def test_procedural_memory_finds_skills_by_keyword_and_tag() -> None:
    """技能检索应同时支持关键词与标签过滤。"""

    async def scenario() -> None:
        memory = InMemoryProceduralMemory()
        simulation = SkillEntry(
            "run_simulation",
            "运行 LAMMPS 分子动力学模拟",
            steps=("准备输入", "提交任务"),
            tags=("simulation", "lammps"),
        )
        analysis = SkillEntry("analyze_results", "分析模拟输出数据", tags=("analysis",))
        await memory.register_skill(simulation)
        await memory.register_skill(analysis)

        assert await memory.find_skills(keyword="lammps") == (simulation,)
        assert await memory.find_skills(tag="analysis") == (analysis,)
        assert await memory.find_skills(keyword="模拟", tag="simulation") == (simulation,)
        assert await memory.find_skills(keyword="模拟", tag="missing") == ()
        assert len(await memory.find_skills()) == 2

    asyncio.run(scenario())


def test_procedural_memory_finds_workflows_by_keyword() -> None:
    """流程模板应按名称或目标模式关键词检索。"""

    async def scenario() -> None:
        memory = InMemoryProceduralMemory()
        template = WorkflowTemplate(
            "tg_workflow",
            goal_pattern="计算玻璃化温度",
            steps=("建模", "平衡", "降温扫描"),
            metadata={"engine": "lammps"},
        )
        other = WorkflowTemplate("etl_workflow", goal_pattern="同步数据库")
        await memory.register_workflow(template)
        await memory.register_workflow(other)

        assert await memory.find_workflows(keyword="玻璃化") == (template,)
        assert await memory.find_workflows(keyword="etl") == (other,)
        assert len(await memory.find_workflows()) == 2

    asyncio.run(scenario())


def test_tool_stats_track_success_rate_and_reasons() -> None:
    """工具统计应正确累计成功率并去重失败原因。"""

    async def scenario() -> None:
        memory = InMemoryProceduralMemory()
        await memory.record_tool_outcome("lammps", True)
        await memory.record_tool_outcome("lammps", True)
        await memory.record_tool_outcome("lammps", False, reason="力场缺失")
        stat = await memory.record_tool_outcome("lammps", False, reason="力场缺失")

        assert stat.success_count == 2
        assert stat.failure_count == 2
        assert stat.success_rate == 0.5
        assert stat.failure_reasons == ("力场缺失",)

        loaded = await memory.tool_stats("lammps")
        assert loaded is not None
        assert loaded.success_rate == 0.5
        assert await memory.tool_stats("missing") is None
        with pytest.raises(ValueError):
            await memory.record_tool_outcome(" ", True)

    asyncio.run(scenario())


def test_tool_stats_copies_do_not_leak_internal_state() -> None:
    """外部修改返回的统计副本不应影响存储内数据。"""

    async def scenario() -> None:
        memory = InMemoryProceduralMemory()
        stat = await memory.record_tool_outcome("rdkit", True)
        stat.success_count = 100

        loaded = await memory.tool_stats("rdkit")
        assert loaded is not None
        assert loaded.success_count == 1

    asyncio.run(scenario())


def test_recommend_tools_orders_by_success_rate() -> None:
    """工具推荐应按成功率降序、调用次数降序排序。"""

    async def scenario() -> None:
        memory = InMemoryProceduralMemory()
        await memory.record_tool_outcome("reliable", True)
        await memory.record_tool_outcome("reliable", True)
        await memory.record_tool_outcome("flaky", True)
        await memory.record_tool_outcome("flaky", False, reason="超时")
        await memory.record_tool_outcome("broken", False)

        ranked = await memory.recommend_tools(limit=3)
        assert [stat.tool_name for stat in ranked] == ["reliable", "flaky", "broken"]
        assert ranked[0].success_rate == 1.0
        assert ranked[1].success_rate == 0.5
        assert ranked[2].success_rate == 0.0

        top = await memory.recommend_tools(limit=1)
        assert [stat.tool_name for stat in top] == ["reliable"]

    asyncio.run(scenario())


def test_best_practices_filter_by_tag() -> None:
    """最佳实践应按 applies_to 标签过滤。"""

    async def scenario() -> None:
        memory = InMemoryProceduralMemory()
        practice = BestPractice(
            "平衡校验",
            "运行生产前先确认体系已达到平衡",
            applies_to=("simulation",),
        )
        general = BestPractice("记录参数", "所有任务都应记录输入参数")
        await memory.add_best_practice(practice)
        await memory.add_best_practice(general)

        assert await memory.find_best_practices(tag="simulation") == (practice,)
        assert await memory.find_best_practices(tag="missing") == ()
        assert len(await memory.find_best_practices()) == 2

    asyncio.run(scenario())


def test_tool_usage_stat_success_rate_defaults_to_zero() -> None:
    """无调用记录时成功率应为零。"""
    stat = ToolUsageStat("unused")
    assert stat.success_rate == 0.0
