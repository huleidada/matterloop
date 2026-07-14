"""组件规格、工厂目录与热插拔注册中心公共入口。"""

from matterloop_core.registry.components import (
    ComponentRegistry,
    ComponentSpec,
    FactoryCatalog,
    PluginDefinition,
)

__all__ = ["ComponentRegistry", "ComponentSpec", "FactoryCatalog", "PluginDefinition"]
