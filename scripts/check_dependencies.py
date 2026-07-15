"""校验 workspace 清单、声明依赖、源码导入、配置注入和固定依赖方向。"""

from __future__ import annotations

import ast
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PACKAGE_ROOTS: dict[str, str] = {
    "matterloop-core": "matterloop_core",
    "matterloop-models": "matterloop_models",
    "matterloop-runtime": "matterloop_runtime",
    "matterloop-tools": "matterloop_tools",
    "matterloop-memory": "matterloop_memory",
    "matterloop-policies": "matterloop_policies",
    "matterloop-agents": "matterloop_agents",
    "matterloop-observability": "matterloop_observability",
    "matterloop-presets": "matterloop_presets",
    "matterloop-integration-fastapi": "matterloop_integration_fastapi",
    "matterloop-integration-celery": "matterloop_integration_celery",
    "matterloop-integration-redis": "matterloop_integration_redis",
}

ALLOWED: dict[str, frozenset[str]] = {
    "matterloop_core": frozenset(),
    "matterloop_models": frozenset(),
    "matterloop_runtime": frozenset({"matterloop_core"}),
    "matterloop_tools": frozenset({"matterloop_runtime"}),
    "matterloop_memory": frozenset({"matterloop_core"}),
    "matterloop_policies": frozenset({"matterloop_core", "matterloop_models", "matterloop_tools"}),
    "matterloop_agents": frozenset(
        {"matterloop_core", "matterloop_models", "matterloop_tools", "matterloop_memory"}
    ),
    "matterloop_observability": frozenset({"matterloop_core"}),
    "matterloop_presets": frozenset(
        {
            "matterloop_core",
            "matterloop_models",
            "matterloop_runtime",
            "matterloop_tools",
            "matterloop_memory",
            "matterloop_policies",
            "matterloop_agents",
            "matterloop_observability",
        }
    ),
    "matterloop_integration_fastapi": frozenset({"matterloop_core", "matterloop_runtime"}),
    "matterloop_integration_celery": frozenset({"matterloop_core", "matterloop_runtime"}),
    "matterloop_integration_redis": frozenset({"matterloop_core", "matterloop_runtime"}),
}

EXTERNAL_IMPORT_DISTRIBUTIONS: dict[str, str] = {
    "celery": "celery",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "mcp": "mcp",
    "openai": "openai",
    "opentelemetry": "opentelemetry-api",
    "pydantic": "pydantic",
    "redis": "redis",
}

CALLER_CONSTRUCTED_CLIENTS: dict[str, frozenset[str]] = {
    "matterloop_models": frozenset({"openai"}),
    "matterloop_integration_redis": frozenset({"redis"}),
}


def imported_roots(path: Path) -> set[str]:
    """提取静态导入及字面量 ``import_module`` 的顶层模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.Call) and _is_dynamic_import(node):
            module_name = cast(ast.Constant, node.args[0]).value
            assert isinstance(module_name, str)
            roots.add(module_name.split(".", maxsplit=1)[0])
    return roots


def reads_process_environment(path: Path) -> bool:
    """判断发行包源码是否直接读取进程环境。

    MatterLoop 是可复用组件库，连接信息和凭据必须由应用层读取并通过构造参数注入。
    这里同时识别常见的 ``os`` 别名、dotenv、依赖 HOME 的 ``expanduser()``，以及未关闭
    ``trust_env`` 的 HTTPX 客户端，避免架构约束只依赖代码审查。

    Args:
        path: 待检查的 Python 源文件。

    Returns:
        发现直接或隐式环境读取时返回 ``True``。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    os_aliases: set[str] = set()
    httpx_aliases: set[str] = set()
    environment_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
                if alias.name == "httpx":
                    httpx_aliases.add(alias.asname or "httpx")
                if alias.name.split(".", maxsplit=1)[0] in {
                    "decouple",
                    "dotenv",
                    "environs",
                    "pydantic_settings",
                }:
                    return True
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", maxsplit=1)[0]
            if root in {"decouple", "dotenv", "environs", "pydantic_settings"}:
                return True
            if node.module == "os":
                for alias in node.names:
                    if alias.name in {
                        "environ",
                        "environb",
                        "get_exec_path",
                        "getenv",
                        "getenvb",
                    }:
                        environment_names.add(alias.asname or alias.name)
            if node.module == "os.path":
                for alias in node.names:
                    if alias.name == "expandvars":
                        environment_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in environment_names:
            return True
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
            and node.attr in {"environ", "environb", "get_exec_path", "getenv", "getenvb"}
        ):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"expanduser", "expandvars"}:
                return True
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in httpx_aliases
                and node.func.attr in {"AsyncClient", "Client"}
            ):
                trust_env = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "trust_env"),
                    None,
                )
                if not isinstance(trust_env, ast.Constant) or trust_env.value is not False:
                    return True
    return False


def _is_dynamic_import(node: ast.Call) -> bool:
    """识别参数为字符串字面量的 ``importlib.import_module`` 调用。"""
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return False
    if not isinstance(node.args[0].value, str):
        return False
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
        and function.attr == "import_module"
    ) or (isinstance(function, ast.Name) and function.id == "__import__")


def _load_pyproject(path: Path) -> Mapping[str, object]:
    """读取一个 TOML 项目文件。"""
    with path.open("rb") as stream:
        return cast(Mapping[str, object], tomllib.load(stream))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, object], value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _requirements(pyproject: Mapping[str, object]) -> tuple[Requirement, ...]:
    """解析基础依赖和全部功能 extra 的依赖声明。"""
    project = _mapping(pyproject.get("project"))
    values = list(_strings(project.get("dependencies")))
    for optional_values in _mapping(project.get("optional-dependencies")).values():
        values.extend(_strings(optional_values))
    requirements: list[Requirement] = []
    for value in values:
        try:
            requirements.append(Requirement(value))
        except InvalidRequirement as exc:
            raise ValueError(f"invalid requirement {value!r}") from exc
    return tuple(requirements)


def _validate_workspace(repository: Path, violations: list[str]) -> None:
    """保证新增或删除发行包时必须同步架构清单。"""
    discovered = {path.parent.name for path in repository.glob("matterloop-*/pyproject.toml")}
    expected = set(PACKAGE_ROOTS)
    for distribution in sorted(discovered - expected):
        violations.append(f"{distribution}: workspace 包未加入依赖检查清单")
    for distribution in sorted(expected - discovered):
        violations.append(f"{distribution}: 依赖检查清单中的发行包不存在")


def _validate_package(
    repository: Path,
    distribution: str,
    import_root: str,
    violations: list[str],
) -> None:
    """校验单个发行包的布局、声明和源码导入。"""
    package_root = repository / distribution
    source_root = package_root / "src" / "python"
    import_path = source_root / import_root
    required_paths = (
        package_root / "README.md",
        import_path / "__init__.py",
        import_path / "py.typed",
    )
    for path in required_paths:
        if not path.is_file():
            violations.append(f"{path.relative_to(repository)}: 必需文件不存在")
    if (source_root / "matterloop").exists():
        violations.append(f"{distribution}: 禁止创建 src/python/matterloop 中间目录")
    if not import_path.is_dir():
        violations.append(f"{distribution}: 源码包目录不存在: {import_root}")
        return

    pyproject = _load_pyproject(package_root / "pyproject.toml")
    project = _mapping(pyproject.get("project"))
    if project.get("name") != distribution:
        violations.append(f"{distribution}: project.name 必须与目录名一致")
    if not str(project.get("version", "")).startswith("0.1."):
        violations.append(f"{distribution}: 版本必须保持在 0.1.x")
    wheel = _mapping(_mapping(_mapping(pyproject.get("tool")).get("hatch")).get("build"))
    targets = _mapping(wheel.get("targets"))
    configured_packages = _strings(_mapping(targets.get("wheel")).get("packages"))
    expected_package = f"src/python/{import_root}"
    if configured_packages != (expected_package,):
        violations.append(f"{distribution}: wheel packages 必须是 {expected_package}")

    try:
        requirements = _requirements(pyproject)
    except ValueError as exc:
        violations.append(f"{distribution}: {exc}")
        return
    declared_names = {canonicalize_name(requirement.name) for requirement in requirements}
    internal_names = {canonicalize_name(name) for name in PACKAGE_ROOTS}
    declared_internal = declared_names & internal_names
    distribution_by_canonical = {canonicalize_name(name): name for name in PACKAGE_ROOTS}
    declared_internal_roots = {
        PACKAGE_ROOTS[distribution_by_canonical[name]] for name in declared_internal
    }

    imported_by_file: dict[Path, set[str]] = {
        path: imported_roots(path) for path in import_path.rglob("*.py")
    }
    for path in imported_by_file:
        if reads_process_environment(path):
            violations.append(
                f"{path.relative_to(repository)}: 库源码禁止读取进程环境，必须由调用方显式注入"
            )
    all_imports = set().union(*imported_by_file.values()) if imported_by_file else set()
    for imported_root in sorted(
        all_imports & CALLER_CONSTRUCTED_CLIENTS.get(import_root, frozenset())
    ):
        violations.append(
            f"{distribution}: 禁止在库内导入 {imported_root} 构造客户端，必须由调用方注入"
        )
    matterloop_roots = set(PACKAGE_ROOTS.values())
    actual_internal = (all_imports & matterloop_roots) - {import_root}
    missing = actual_internal - declared_internal_roots
    extra = declared_internal_roots - actual_internal
    forbidden = declared_internal_roots - ALLOWED[import_root]
    for dependency in sorted(missing):
        violations.append(f"{distribution}: 导入 {dependency} 但未声明对应发行包")
    for dependency in sorted(extra):
        violations.append(f"{distribution}: 声明了未使用的内部依赖 {dependency}")
    for dependency in sorted(forbidden):
        violations.append(f"{distribution}: pyproject 声明了禁止方向 {dependency}")

    for requirement in requirements:
        normalized_name = canonicalize_name(requirement.name)
        if normalized_name not in internal_names:
            continue
        specifiers = {str(specifier) for specifier in requirement.specifier}
        if specifiers != {">=0.1.0", "<0.2.0"} or requirement.marker is not None:
            violations.append(
                f"{distribution}: 内部依赖 {requirement.name} 必须使用 >=0.1.0,<0.2.0"
            )

    for imported_root, required_distribution in EXTERNAL_IMPORT_DISTRIBUTIONS.items():
        if (
            imported_root in all_imports
            and canonicalize_name(required_distribution) not in declared_names
        ):
            violations.append(
                f"{distribution}: 导入 {imported_root} 但未直接声明 {required_distribution}"
            )

    for path, imports in imported_by_file.items():
        invalid = (imports & matterloop_roots) - ALLOWED[import_root] - {import_root}
        if invalid:
            values = ", ".join(sorted(invalid))
            violations.append(f"{path.relative_to(repository)}: 禁止依赖 {values}")


def main() -> int:
    """扫描全部发行包并返回进程退出码。"""
    repository = Path(__file__).resolve().parent.parent
    violations: list[str] = []
    _validate_workspace(repository, violations)
    for distribution, import_root in PACKAGE_ROOTS.items():
        _validate_package(repository, distribution, import_root, violations)
    if violations:
        print("\n".join(violations))
        return 1
    print(f"依赖边界检查通过：{len(PACKAGE_ROOTS)} 个发行包")
    return 0


if __name__ == "__main__":
    sys.exit(main())
