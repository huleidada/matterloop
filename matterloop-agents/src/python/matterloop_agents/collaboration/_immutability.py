"""协作值对象使用的递归复制与冻结辅助函数。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType


def freeze_value(value: object) -> object:
    """递归冻结常见容器，并复制其他对象以隔离调用方引用。"""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_value(item) for item in value)
    return deepcopy(value)


def freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """复制并递归冻结字符串键映射。"""
    return MappingProxyType({key: freeze_value(item) for key, item in value.items()})


__all__ = ["freeze_mapping", "freeze_value"]
