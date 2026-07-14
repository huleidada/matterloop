"""验证构建后的发行包可以独立导入并包含类型标记。"""

from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import pkgutil
import sys
from types import ModuleType

IMPORT_ROOTS = (
    "matterloop_core",
    "matterloop_models",
    "matterloop_runtime",
    "matterloop_tools",
    "matterloop_memory",
    "matterloop_policies",
    "matterloop_agents",
    "matterloop_observability",
    "matterloop_presets",
    "matterloop_integration_fastapi",
    "matterloop_integration_celery",
    "matterloop_integration_redis",
)

RUNTIME_DEPENDENCY_SMOKES: dict[str, tuple[str, ...]] = {
    "matterloop_tools": ("httpx",),
    "matterloop_integration_fastapi": ("fastapi", "pydantic"),
    "matterloop_integration_celery": ("celery",),
    "matterloop_integration_redis": ("redis.asyncio",),
}


def _verify_module(module_name: str) -> None:
    """导入完整模块树，检查稳定导出和 PEP 561 标记。"""
    module = importlib.import_module(module_name)
    marker = importlib.resources.files(module_name).joinpath("py.typed")
    if not marker.is_file():
        raise RuntimeError(f"py.typed is missing from installed package: {module_name}")
    _verify_exports(module)
    package_path = getattr(module, "__path__", ())
    for module_info in pkgutil.walk_packages(package_path, prefix=f"{module_name}."):
        imported = importlib.import_module(module_info.name)
        _verify_exports(imported)
    for dependency in RUNTIME_DEPENDENCY_SMOKES.get(module_name, ()):
        importlib.import_module(dependency)


def _verify_exports(module: ModuleType) -> None:
    """保证模块声明的 ``__all__`` 全部可以解析。"""
    exports = getattr(module, "__all__", ())
    if not isinstance(exports, (tuple, list)) or not all(isinstance(name, str) for name in exports):
        raise RuntimeError(f"invalid __all__ in installed module: {module.__name__}")
    for name in exports:
        getattr(module, name)


def main(arguments: list[str]) -> int:
    """导入指定模块；未指定时验证全部稳定入口。"""
    for forbidden_name in ("core", "matterloop"):
        if importlib.util.find_spec(forbidden_name) is not None:
            raise RuntimeError(f"legacy or namespace package must not exist: {forbidden_name}")
    modules = tuple(arguments) if arguments else IMPORT_ROOTS
    unknown = set(modules) - set(IMPORT_ROOTS)
    if unknown:
        values = ", ".join(sorted(unknown))
        raise ValueError(f"unknown MatterLoop import roots: {values}")
    for module_name in modules:
        _verify_module(module_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
