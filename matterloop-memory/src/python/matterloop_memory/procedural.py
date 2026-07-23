"""技能、流程模板与工具经验的 Procedural Memory 实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """表示一项可复用的技能。"""

    name: str
    description: str
    steps: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    skill_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        """校验必填字段。"""
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not self.description.strip():
            raise ValueError("description must not be empty")


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    """表示一套可复用的任务流程模板。"""

    name: str
    goal_pattern: str
    steps: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    template_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        """校验必填字段并冻结元数据。"""
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not self.goal_pattern.strip():
            raise ValueError("goal_pattern must not be empty")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(slots=True)
class ToolUsageStat:
    """记录单个工具的调用结果统计。"""

    tool_name: str
    success_count: int = 0
    failure_count: int = 0
    failure_reasons: tuple[str, ...] = ()

    @property
    def success_rate(self) -> float:
        """返回成功率；无调用记录时为零。"""
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 0.0


@dataclass(frozen=True, slots=True)
class BestPractice:
    """表示一条最佳实践。"""

    title: str
    guidance: str
    applies_to: tuple[str, ...] = ()
    practice_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        """校验必填字段。"""
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if not self.guidance.strip():
            raise ValueError("guidance must not be empty")


@runtime_checkable
class ProceduralMemoryStore(Protocol):
    """Procedural Memory 存储扩展协议。"""

    async def register_skill(self, skill: SkillEntry) -> None:
        """注册或替换一项技能。"""
        ...

    async def find_skills(
        self, keyword: str | None = None, tag: str | None = None, limit: int = 10
    ) -> tuple[SkillEntry, ...]:
        """按关键词与标签检索技能。"""
        ...

    async def register_workflow(self, template: WorkflowTemplate) -> None:
        """注册或替换一套流程模板。"""
        ...

    async def find_workflows(
        self, keyword: str | None = None, limit: int = 10
    ) -> tuple[WorkflowTemplate, ...]:
        """按关键词检索流程模板。"""
        ...

    async def record_tool_outcome(
        self, tool_name: str, success: bool, reason: str | None = None
    ) -> ToolUsageStat:
        """记录一次工具调用结果并返回最新统计。"""
        ...

    async def tool_stats(self, tool_name: str) -> ToolUsageStat | None:
        """读取指定工具的统计信息。"""
        ...

    async def recommend_tools(self, limit: int = 5) -> tuple[ToolUsageStat, ...]:
        """按成功率降序推荐工具。"""
        ...

    async def add_best_practice(self, practice: BestPractice) -> None:
        """新增或替换一条最佳实践。"""
        ...

    async def find_best_practices(
        self, tag: str | None = None, limit: int = 10
    ) -> tuple[BestPractice, ...]:
        """按标签检索最佳实践。"""
        ...


class InMemoryProceduralMemory:
    """并发安全的 Procedural Memory 内存实现。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillEntry] = {}
        self._workflows: dict[str, WorkflowTemplate] = {}
        self._tool_stats: dict[str, ToolUsageStat] = {}
        self._practices: dict[str, BestPractice] = {}
        self._lock = asyncio.Lock()

    async def register_skill(self, skill: SkillEntry) -> None:
        """注册或替换一项技能。

        Args:
            skill: 待注册的技能条目。
        """
        async with self._lock:
            self._skills[skill.skill_id] = skill

    async def find_skills(
        self, keyword: str | None = None, tag: str | None = None, limit: int = 10
    ) -> tuple[SkillEntry, ...]:
        """按关键词与标签检索技能。

        Args:
            keyword: 名称或描述需要包含的关键词；None 表示不过滤。
            tag: 技能标签；None 表示不过滤。
            limit: 返回结果数量上限。

        Returns:
            满足全部给定条件的技能，按名称与标识排序。

        Raises:
            ValueError: limit 小于一。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        async with self._lock:
            skills = tuple(self._skills.values())
        selected = [
            skill
            for skill in skills
            if _keyword_matches(keyword, skill.name, skill.description)
            and (tag is None or tag in skill.tags)
        ]
        selected.sort(key=lambda skill: (skill.name, skill.skill_id))
        return tuple(selected[:limit])

    async def register_workflow(self, template: WorkflowTemplate) -> None:
        """注册或替换一套流程模板。

        Args:
            template: 待注册的流程模板。
        """
        async with self._lock:
            self._workflows[template.template_id] = template

    async def find_workflows(
        self, keyword: str | None = None, limit: int = 10
    ) -> tuple[WorkflowTemplate, ...]:
        """按关键词检索流程模板。

        Args:
            keyword: 名称或目标模式需要包含的关键词；None 表示不过滤。
            limit: 返回结果数量上限。

        Returns:
            满足条件的流程模板，按名称与标识排序。

        Raises:
            ValueError: limit 小于一。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        async with self._lock:
            templates = tuple(self._workflows.values())
        selected = [
            template
            for template in templates
            if _keyword_matches(keyword, template.name, template.goal_pattern)
        ]
        selected.sort(key=lambda template: (template.name, template.template_id))
        return tuple(selected[:limit])

    async def record_tool_outcome(
        self, tool_name: str, success: bool, reason: str | None = None
    ) -> ToolUsageStat:
        """记录一次工具调用结果。

        Args:
            tool_name: 工具名称。
            success: 本次调用是否成功。
            reason: 失败原因；仅在失败时记录，重复原因不再追加。

        Returns:
            更新后的统计信息副本。

        Raises:
            ValueError: tool_name 为空。
        """
        if not tool_name.strip():
            raise ValueError("tool_name must not be empty")
        async with self._lock:
            stat = self._tool_stats.get(tool_name)
            if stat is None:
                stat = ToolUsageStat(tool_name)
                self._tool_stats[tool_name] = stat
            if success:
                stat.success_count += 1
            else:
                stat.failure_count += 1
                if reason is not None and reason.strip() and reason not in stat.failure_reasons:
                    stat.failure_reasons = (*stat.failure_reasons, reason)
            return replace(stat)

    async def tool_stats(self, tool_name: str) -> ToolUsageStat | None:
        """读取指定工具的统计信息。

        Args:
            tool_name: 工具名称。

        Returns:
            统计信息副本；无记录时返回 None。
        """
        async with self._lock:
            stat = self._tool_stats.get(tool_name)
            return replace(stat) if stat is not None else None

    async def recommend_tools(self, limit: int = 5) -> tuple[ToolUsageStat, ...]:
        """按成功率降序推荐工具。

        Args:
            limit: 返回结果数量上限。

        Returns:
            按成功率降序、调用次数降序、名称升序排序的统计副本。

        Raises:
            ValueError: limit 小于一。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        async with self._lock:
            stats = [replace(stat) for stat in self._tool_stats.values()]
        stats.sort(
            key=lambda stat: (
                -stat.success_rate,
                -(stat.success_count + stat.failure_count),
                stat.tool_name,
            )
        )
        return tuple(stats[:limit])

    async def add_best_practice(self, practice: BestPractice) -> None:
        """新增或替换一条最佳实践。

        Args:
            practice: 待写入的最佳实践。
        """
        async with self._lock:
            self._practices[practice.practice_id] = practice

    async def find_best_practices(
        self, tag: str | None = None, limit: int = 10
    ) -> tuple[BestPractice, ...]:
        """按标签检索最佳实践。

        Args:
            tag: applies_to 需要包含的标签；None 表示不过滤。
            limit: 返回结果数量上限。

        Returns:
            满足条件的最佳实践，按标题与标识排序。

        Raises:
            ValueError: limit 小于一。
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        async with self._lock:
            practices = tuple(self._practices.values())
        selected = [practice for practice in practices if tag is None or tag in practice.applies_to]
        selected.sort(key=lambda practice: (practice.title, practice.practice_id))
        return tuple(selected[:limit])


def _keyword_matches(keyword: str | None, *texts: str) -> bool:
    """判断任一文本是否包含关键词（大小写不敏感）。"""
    if keyword is None or not keyword.strip():
        return True
    needle = keyword.casefold()
    return any(needle in text.casefold() for text in texts)
