"""基于不可变快照的线程安全 Skill 注册表。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from threading import RLock
from types import MappingProxyType

from matterloop_tools.skills.base import SkillContent, SkillSpec, validate_skill_name
from matterloop_tools.skills.errors import (
    SkillExistsError,
    SkillNotFoundError,
)
from matterloop_tools.skills.loader import SkillLoader


class SkillRegistry:
    """提供 Skill 注册、发现、获取、热替换和全量原子刷新。

    Skill 内容是不可变值对象，因此读取方取得旧对象后可以安全完成当前操作；替换和刷新
    只影响之后的读取。全量刷新会先在锁外完成全部加载和校验，再在锁内一次替换快照。

    Args:
        skills: 初始 Skill 内容。
    """

    def __init__(self, skills: Iterable[SkillContent] = ()) -> None:
        initial = self._build_snapshot(skills)
        self._snapshot: Mapping[str, SkillContent] = MappingProxyType(initial)
        self._lock = RLock()

    def register(self, skill: SkillContent, *, replace: bool = False) -> None:
        """注册 Skill，并在显式允许时原子替换同名内容。

        Args:
            skill: 待注册的不可变 Skill 内容。
            replace: 是否允许替换同名 Skill。

        Raises:
            SkillExistsError: 同名 Skill 已存在且没有允许替换。
        """
        with self._lock:
            if skill.spec.name in self._snapshot and not replace:
                raise SkillExistsError(skill.spec.name)
            updated = dict(self._snapshot)
            updated[skill.spec.name] = skill
            self._snapshot = MappingProxyType(updated)

    def replace(self, name: str, skill: SkillContent) -> None:
        """热替换已存在的同名 Skill。

        Args:
            name: 已注册名称。
            skill: 新内容，规范名称必须与 ``name`` 一致。

        Raises:
            SkillNotFoundError: 目标不存在。
            ValueError: 新内容名称与目标名称不一致。
        """
        validate_skill_name(name)
        if skill.spec.name != name:
            raise ValueError("replacement skill name must match registry name")
        with self._lock:
            if name not in self._snapshot:
                raise SkillNotFoundError(name)
            updated = dict(self._snapshot)
            updated[name] = skill
            self._snapshot = MappingProxyType(updated)

    def unregister(self, name: str) -> None:
        """原子移除一个 Skill。

        Args:
            name: 待移除名称。

        Raises:
            SkillNotFoundError: Skill 不存在。
        """
        validate_skill_name(name)
        with self._lock:
            if name not in self._snapshot:
                raise SkillNotFoundError(name)
            updated = dict(self._snapshot)
            del updated[name]
            self._snapshot = MappingProxyType(updated)

    def get(self, name: str) -> SkillContent:
        """从当前快照获取 Skill 内容。

        Args:
            name: Skill 名称。

        Returns:
            当前不可变 Skill 内容。

        Raises:
            SkillNotFoundError: Skill 不存在。
        """
        validate_skill_name(name)
        with self._lock:
            snapshot = self._snapshot
        try:
            return snapshot[name]
        except KeyError as exc:
            raise SkillNotFoundError(name) from exc

    def names(self) -> tuple[str, ...]:
        """返回当前快照中的稳定排序名称。"""
        with self._lock:
            snapshot = self._snapshot
        return tuple(sorted(snapshot))

    def discover(self) -> tuple[SkillSpec, ...]:
        """返回当前快照中按名称排序的发现元数据。"""
        with self._lock:
            snapshot = self._snapshot
        return tuple(snapshot[name].spec for name in sorted(snapshot))

    def refresh(self, loader: SkillLoader) -> tuple[SkillSpec, ...]:
        """从加载器全量原子刷新注册表。

        加载或校验任意 Skill 失败时保留原快照；成功时一次替换全部内容，包括移除磁盘上
        已不存在的 Skill。

        Args:
            loader: 本地安全加载器。

        Returns:
            刷新后的稳定排序发现元数据。
        """
        loaded = loader.discover()
        updated = self._build_snapshot(loaded)
        with self._lock:
            self._snapshot = MappingProxyType(updated)
        return self.discover()

    @staticmethod
    def _build_snapshot(skills: Iterable[SkillContent]) -> dict[str, SkillContent]:
        snapshot: dict[str, SkillContent] = {}
        for skill in skills:
            name = skill.spec.name
            if name in snapshot:
                raise SkillExistsError(name)
            snapshot[name] = skill
        return snapshot
