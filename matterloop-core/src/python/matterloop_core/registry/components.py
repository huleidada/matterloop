"""支持工厂发现与运行时原子替换的组件注册设施。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from threading import RLock
from types import MappingProxyType
from typing import Generic, TypeVar, cast

from matterloop_core.exceptions import (
    ComponentAlreadyRegisteredError,
    ComponentNotFoundError,
    InvalidPluginError,
)

TComponent = TypeVar("TComponent")


@dataclass(frozen=True, slots=True)
class ComponentSpec(Generic[TComponent]):
    """描述一个可以延迟创建的具名组件。

    Args:
        name: 在对应注册中心内唯一的稳定名称。
        factory: 每次调用都返回一个已配置组件实例的无参工厂。
        version: 组件自身的语义版本。
        capabilities: 供装配层筛选组件使用的能力标签。
        description: 面向使用者的简短说明。
        metadata: 插件自定义的只读字符串元数据。
    """

    name: str
    factory: Callable[[], TComponent] = field(repr=False, compare=False)
    version: str = "0.1.0"
    capabilities: frozenset[str] = frozenset()
    description: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范化标识和集合，防止插件目录在注册后发生变化。"""
        normalized_name = self.name.strip()
        normalized_version = self.version.strip()
        if not normalized_name:
            raise ValueError("component name must not be empty")
        if not normalized_version:
            raise ValueError("component version must not be empty")
        if any(not capability.strip() for capability in self.capabilities):
            raise ValueError("capabilities must not contain empty values")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "version", normalized_version)
        object.__setattr__(
            self,
            "capabilities",
            frozenset(capability.strip() for capability in self.capabilities),
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class PluginDefinition(Generic[TComponent]):
    """声明一个第三方插件贡献的组件工厂集合。"""

    name: str
    version: str
    components: tuple[ComponentSpec[TComponent], ...]

    def __post_init__(self) -> None:
        """确保插件标识有效且内部没有重复组件名称。"""
        normalized_name = self.name.strip()
        normalized_version = self.version.strip()
        if not normalized_name:
            raise ValueError("plugin name must not be empty")
        if not normalized_version:
            raise ValueError("plugin version must not be empty")
        if not self.components:
            raise ValueError("plugin must contain at least one component")
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("plugin contains duplicate component names")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "version", normalized_version)


class FactoryCatalog(Generic[TComponent]):
    """保存组件工厂，并通过显式调用发现第三方插件。"""

    def __init__(self, entry_point_group: str | None = None) -> None:
        self._specs: dict[str, ComponentSpec[TComponent]] = {}
        self._entry_point_group = entry_point_group
        self._lock = RLock()

    def register(self, spec: ComponentSpec[TComponent], *, replace: bool = False) -> None:
        """注册或显式替换一个组件工厂。"""
        with self._lock:
            if spec.name in self._specs and not replace:
                raise ComponentAlreadyRegisteredError(spec.name)
            self._specs[spec.name] = spec

    def install(
        self, plugin: PluginDefinition[TComponent], *, replace: bool = False
    ) -> tuple[str, ...]:
        """以单次原子更新安装插件贡献的全部工厂。"""
        incoming = {spec.name: spec for spec in plugin.components}
        with self._lock:
            if not replace:
                duplicate = next((name for name in incoming if name in self._specs), None)
                if duplicate is not None:
                    raise ComponentAlreadyRegisteredError(duplicate)
            self._specs.update(incoming)
        return tuple(incoming)

    def unregister(self, name: str) -> ComponentSpec[TComponent]:
        """移除并返回工厂规格。"""
        with self._lock:
            try:
                return self._specs.pop(name)
            except KeyError as exc:
                raise ComponentNotFoundError(name) from exc

    def spec(self, name: str) -> ComponentSpec[TComponent]:
        """返回指定组件的不可变规格。"""
        with self._lock:
            try:
                return self._specs[name]
            except KeyError as exc:
                raise ComponentNotFoundError(name) from exc

    def create(self, name: str) -> TComponent:
        """调用当前工厂创建一个新组件实例。"""
        return self.spec(name).factory()

    def names(self) -> tuple[str, ...]:
        """返回稳定且已排序的工厂名称快照。"""
        with self._lock:
            return tuple(sorted(self._specs))

    def discover(self, *, replace: bool = False) -> tuple[str, ...]:
        """从 Entry Point 加载 ``PluginDefinition`` 并安装其工厂。

        Entry Point 可以直接导出插件定义，也可以导出返回插件定义的无参函数。插件发现
        必须由调用方显式触发，普通导入不会执行第三方代码。
        """
        if self._entry_point_group is None:
            return ()
        installed: list[str] = []
        for entry_point in entry_points(group=self._entry_point_group):
            loaded = cast(object, entry_point.load())
            candidate = cast(Callable[[], object], loaded)() if callable(loaded) else loaded
            if not isinstance(candidate, PluginDefinition):
                raise InvalidPluginError(
                    f"entry point {entry_point.name!r} must return PluginDefinition"
                )
            plugin = cast(PluginDefinition[TComponent], candidate)
            installed.extend(self.install(plugin, replace=replace))
        return tuple(installed)


class ComponentRegistry(Generic[TComponent]):
    """保存具名组件实例，并支持运行时原子替换。"""

    def __init__(self, entry_point_group: str | None = None) -> None:
        self._components: dict[str, TComponent] = {}
        self._entry_point_group = entry_point_group
        self._lock = RLock()

    def register(self, name: str, component: TComponent, *, replace: bool = False) -> None:
        """注册组件，或者显式替换已有组件。"""
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("component name must not be empty")
        with self._lock:
            if normalized_name in self._components and not replace:
                raise ComponentAlreadyRegisteredError(normalized_name)
            self._components[normalized_name] = component

    def install(
        self,
        catalog: FactoryCatalog[TComponent],
        *,
        names: tuple[str, ...] | None = None,
        replace: bool = False,
    ) -> tuple[str, ...]:
        """创建选定工厂并以单次原子更新安装所有实例。

        所有工厂会在持有注册锁之前完成。任一工厂失败时，现有注册表保持不变。
        """
        selected = names if names is not None else catalog.names()
        created = {name: catalog.create(name) for name in selected}
        with self._lock:
            if not replace:
                duplicate = next((name for name in created if name in self._components), None)
                if duplicate is not None:
                    raise ComponentAlreadyRegisteredError(duplicate)
            self._components.update(created)
        return tuple(selected)

    def unregister(self, name: str) -> TComponent:
        """移除并返回组件，便于调用方按需清理资源。"""
        with self._lock:
            try:
                return self._components.pop(name)
            except KeyError as exc:
                raise ComponentNotFoundError(name) from exc

    def get(self, name: str) -> TComponent:
        """返回当前注册的组件实例快照。"""
        with self._lock:
            try:
                return self._components[name]
            except KeyError as exc:
                raise ComponentNotFoundError(name) from exc

    def names(self) -> tuple[str, ...]:
        """返回稳定且已排序的组件名称快照。"""
        with self._lock:
            return tuple(sorted(self._components))

    def discover(self, *, replace: bool = False) -> tuple[str, ...]:
        """发现插件工厂并原子安装对应组件实例。"""
        catalog = FactoryCatalog[TComponent](self._entry_point_group)
        names = catalog.discover(replace=replace)
        return self.install(catalog, names=names, replace=replace)
