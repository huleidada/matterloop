"""学习闭环所需的本地结构化记忆协议。

本模块刻意不依赖 ``matterloop_memory``：任何提供同形属性与方法的对象都可以注入，
以便记忆实现独立演进。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EpisodeLike(Protocol):
    """一条历史执行经验的最小只读结构。"""

    @property
    def goal(self) -> str:
        """返回该次执行的目标描述。"""
        ...

    @property
    def succeeded(self) -> bool:
        """返回该次执行是否成功。"""
        ...

    @property
    def failure_summary(self) -> str:
        """返回失败原因摘要；成功经验通常为空字符串。"""
        ...

    @property
    def resolution(self) -> str:
        """返回解决方式或成功路径摘要。"""
        ...

    @property
    def tags(self) -> tuple[str, ...]:
        """返回用于分类检索的标签。"""
        ...


class EpisodeSource(Protocol):
    """按需检索历史经验的只读数据源接口。"""

    async def list_failures(self, limit: int) -> Sequence[EpisodeLike]:
        """返回最多 ``limit`` 条失败经验。"""
        ...

    async def list_successes(self, limit: int) -> Sequence[EpisodeLike]:
        """返回最多 ``limit`` 条成功经验。"""
        ...

    async def find_similar(self, goal: str, limit: int) -> Sequence[EpisodeLike]:
        """返回与目标最相似的最多 ``limit`` 条经验。"""
        ...


__all__ = ["EpisodeLike", "EpisodeSource"]
