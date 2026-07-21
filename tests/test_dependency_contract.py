"""依赖声明、源码导入和同步版本区间的工作区级契约测试。"""

from __future__ import annotations

from pathlib import Path

from scripts import check_dependencies


def _create_runtime_package(repository: Path, *, version: str, core_requirement: str) -> None:
    """创建仅依赖 Core 的最小 Runtime 发行包。"""
    package_root = repository / "matterloop-runtime"
    import_root = package_root / "src" / "python" / "matterloop_runtime"
    import_root.mkdir(parents=True)
    (package_root / "README.md").write_text("# Runtime\n", encoding="utf-8")
    (import_root / "__init__.py").write_text(
        'from matterloop_core import LoopRequest\n\n__all__ = ["LoopRequest"]\n',
        encoding="utf-8",
    )
    (import_root / "py.typed").write_text("\n", encoding="utf-8")
    (package_root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                'name = "matterloop-runtime"',
                f'version = "{version}"',
                f'dependencies = ["matterloop-core{core_requirement}"]',
                "",
                "[tool.hatch.build.targets.wheel]",
                'packages = ["src/python/matterloop_runtime"]',
                "",
            )
        ),
        encoding="utf-8",
    )


def _runtime_violations(repository: Path) -> list[str]:
    """执行最小 Runtime 包的依赖边界检查。"""
    violations: list[str] = []
    check_dependencies._validate_package(
        repository,
        "matterloop-runtime",
        "matterloop_runtime",
        violations,
    )
    return violations


def test_internal_dependency_range_tracks_current_patch_version(tmp_path: Path) -> None:
    """补丁版本升级后，内部依赖下界应同步使用当前发行版本。"""
    _create_runtime_package(
        tmp_path,
        version="0.1.1",
        core_requirement=">=0.1.1,<0.2.0",
    )

    assert _runtime_violations(tmp_path) == []


def test_stale_internal_dependency_lower_bound_is_reported(tmp_path: Path) -> None:
    """内部依赖仍指向旧补丁版本时应给出动态期望范围。"""
    _create_runtime_package(
        tmp_path,
        version="0.1.1",
        core_requirement=">=0.1.0,<0.2.0",
    )

    assert _runtime_violations(tmp_path) == [
        "matterloop-runtime: 内部依赖 matterloop-core 必须使用 >=0.1.1,<0.2.0"
    ]
