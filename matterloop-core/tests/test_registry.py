"""可热插拔组件注册中心的单元测试。"""

import pytest
from matterloop_core import (
    ComponentAlreadyRegisteredError,
    ComponentNotFoundError,
    ComponentRegistry,
    ComponentSpec,
    FactoryCatalog,
    PluginDefinition,
)


def test_duplicate_registration_requires_explicit_replace() -> None:
    """重复名称不得静默改变运行时行为。"""
    registry = ComponentRegistry[object]()
    registry.register("worker", object())

    with pytest.raises(ComponentAlreadyRegisteredError):
        registry.register("worker", object())


def test_unregister_returns_component_and_removes_name() -> None:
    """注销后应返回已移除实例，便于调用方清理资源。"""
    component = object()
    registry = ComponentRegistry[object]()
    registry.register("worker", component)

    assert registry.unregister("worker") is component
    assert registry.names() == ()
    with pytest.raises(ComponentNotFoundError):
        registry.get("worker")


def test_plugin_factories_are_installed_and_materialized() -> None:
    """插件定义应先进入工厂目录，再按需原子创建组件实例。"""
    first = object()
    second = object()
    catalog = FactoryCatalog[object]()
    plugin = PluginDefinition(
        name="example",
        version="1.0.0",
        components=(
            ComponentSpec("first", lambda: first, capabilities=frozenset({"execute"})),
            ComponentSpec("second", lambda: second),
        ),
    )

    assert catalog.install(plugin) == ("first", "second")
    registry = ComponentRegistry[object]()
    assert registry.install(catalog) == ("first", "second")
    assert registry.get("first") is first
    assert registry.get("second") is second


def test_factory_failure_does_not_partially_change_registry() -> None:
    """任一工厂失败时，组件注册表必须保持原子性。"""

    def fail() -> object:
        raise RuntimeError("factory failed")

    catalog = FactoryCatalog[object]()
    catalog.register(ComponentSpec("created", object))
    catalog.register(ComponentSpec("failed", fail))
    registry = ComponentRegistry[object]()

    with pytest.raises(RuntimeError, match="factory failed"):
        registry.install(catalog)

    assert registry.names() == ()


def test_replacing_factory_only_affects_new_materialization() -> None:
    """热替换工厂不会改变已返回实例，只影响后续安装。"""
    old = object()
    new = object()
    catalog = FactoryCatalog[object]()
    catalog.register(ComponentSpec("worker", lambda: old))
    registry = ComponentRegistry[object]()
    registry.install(catalog)

    catalog.register(ComponentSpec("worker", lambda: new), replace=True)
    registry.install(catalog, replace=True)

    assert registry.get("worker") is new
