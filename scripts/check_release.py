"""校验 MatterLoop 多发行包在发布前保持一致且具备完整制品材料。"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

DISTRIBUTIONS: tuple[str, ...] = (
    "matterloop-agents",
    "matterloop-core",
    "matterloop-integration-celery",
    "matterloop-integration-fastapi",
    "matterloop-integration-redis",
    "matterloop-memory",
    "matterloop-models",
    "matterloop-observability",
    "matterloop-policies",
    "matterloop-presets",
    "matterloop-runtime",
    "matterloop-tools",
)
CANONICAL_DISTRIBUTIONS = frozenset(canonicalize_name(name) for name in DISTRIBUTIONS)


def _mapping(value: object) -> Mapping[str, object]:
    """将未知 TOML 节点安全收窄为映射。"""
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, object], value)


def _strings(value: object) -> tuple[str, ...]:
    """将 TOML 数组安全收窄为字符串元组。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _read_pyproject(path: Path) -> Mapping[str, object]:
    """读取发行包的 ``pyproject.toml``。"""
    with path.open("rb") as stream:
        return cast(Mapping[str, object], tomllib.load(stream))


def _parse_release_version(value: str, field_name: str, violations: list[str]) -> Version | None:
    """解析只允许三段数字的公开发行版本。"""
    if not VERSION_PATTERN.fullmatch(value):
        violations.append(f"{field_name} 必须使用 X.Y.Z 格式，实际为 {value!r}")
        return None
    try:
        return Version(value)
    except InvalidVersion:
        violations.append(f"{field_name} 不是有效的 Python 版本：{value!r}")
        return None


def _resolve_package_file(package_root: Path, relative_path: str) -> Path | None:
    """解析包内文件，并拒绝通过相对路径逃出发行包构建上下文。"""
    resolved_root = package_root.resolve()
    resolved_path = (package_root / relative_path).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        return None
    return resolved_path


def _readme_path(project: Mapping[str, object]) -> str | None:
    """从 PEP 621 的字符串或表格形式读取 README 文件路径。"""
    readme = project.get("readme")
    if isinstance(readme, str):
        return readme
    file_value = _mapping(readme).get("file")
    return file_value if isinstance(file_value, str) else None


def _requirements(project: Mapping[str, object]) -> tuple[str, ...]:
    """收集基础依赖与全部可选依赖中的需求字符串。"""
    values = list(_strings(project.get("dependencies")))
    for extra_dependencies in _mapping(project.get("optional-dependencies")).values():
        values.extend(_strings(extra_dependencies))
    return tuple(values)


def _validate_workspace(repository: Path, violations: list[str]) -> None:
    """校验工作区恰好包含约定的十二个发行包。"""
    discovered = {path.parent.name for path in repository.glob("matterloop-*/pyproject.toml")}
    expected = set(DISTRIBUTIONS)
    for distribution in sorted(expected - discovered):
        violations.append(f"{distribution}: 缺少发行包或 pyproject.toml")
    for distribution in sorted(discovered - expected):
        violations.append(f"{distribution}: 未登记在发布清单中")


def _validate_materials(
    repository: Path,
    package_root: Path,
    project: Mapping[str, object],
    distribution: str,
    violations: list[str],
) -> None:
    """确认 README、PEP 561 标记和许可证正文位于发行包构建上下文。"""
    readme_value = _readme_path(project)
    if readme_value is None:
        violations.append(f"{distribution}: project.readme 必须引用包内 README 文件")
    else:
        readme_path = _resolve_package_file(package_root, readme_value)
        if readme_path is None:
            violations.append(f"{distribution}: README 不得位于发行包目录之外")
        elif not readme_path.is_file() or readme_path.stat().st_size == 0:
            violations.append(f"{distribution}: README 文件不存在或为空：{readme_value}")

    license_path = package_root / "LICENSE"
    root_license = repository / "LICENSE"
    if not license_path.is_file() or license_path.stat().st_size == 0:
        violations.append(f"{distribution}: 缺少包内 LICENSE，无法保证许可证进入制品")
    elif (
        root_license.is_file()
        and license_path.read_text(encoding="utf-8").rstrip()
        != root_license.read_text(encoding="utf-8").rstrip()
    ):
        violations.append(f"{distribution}: 包内 LICENSE 与仓库根 LICENSE 不一致")
    license_files = _strings(project.get("license-files"))
    if "LICENSE" not in license_files:
        violations.append(f'{distribution}: project.license-files 必须显式包含 "LICENSE"')


def _wheel_package_paths(pyproject: Mapping[str, object]) -> tuple[str, ...]:
    """读取 Hatchling wheel 目标中的源码包路径。"""
    tool = _mapping(pyproject.get("tool"))
    hatch = _mapping(tool.get("hatch"))
    build = _mapping(hatch.get("build"))
    targets = _mapping(build.get("targets"))
    wheel = _mapping(targets.get("wheel"))
    return _strings(wheel.get("packages"))


def _validate_type_markers(
    package_root: Path,
    pyproject: Mapping[str, object],
    distribution: str,
    violations: list[str],
) -> None:
    """确认每个 wheel 源码包都携带 ``py.typed``。"""
    package_paths = _wheel_package_paths(pyproject)
    if not package_paths:
        violations.append(f"{distribution}: 未配置 Hatchling wheel packages")
        return
    for relative_path in package_paths:
        source_path = _resolve_package_file(package_root, relative_path)
        if source_path is None:
            violations.append(f"{distribution}: wheel package 不得位于发行包目录之外")
            continue
        marker = source_path / "py.typed"
        if not marker.is_file():
            violations.append(
                f"{distribution}: wheel 源码包缺少 py.typed：{relative_path}/py.typed"
            )


def _validate_internal_requirements(
    project: Mapping[str, object],
    distribution: str,
    release_version: Version,
    violations: list[str],
) -> None:
    """校验内部依赖严格覆盖本次同步发行版本。"""
    upper_bound = Version(f"{release_version.major}.{release_version.minor + 1}.0")
    expected_specifiers = {f">={release_version}", f"<{upper_bound}"}
    for raw_requirement in _requirements(project):
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            violations.append(f"{distribution}: 无法解析依赖声明 {raw_requirement!r}")
            continue
        dependency = canonicalize_name(requirement.name)
        if not dependency.startswith("matterloop-"):
            continue
        if dependency not in CANONICAL_DISTRIBUTIONS:
            violations.append(f"{distribution}: 引用了未登记的内部依赖 {requirement.name}")
            continue
        actual_specifiers = {str(specifier) for specifier in requirement.specifier}
        covers_release = requirement.specifier.contains(release_version, prereleases=True)
        if (
            actual_specifiers != expected_specifiers
            or not covers_release
            or requirement.marker is not None
            or requirement.url is not None
        ):
            expected = f">={release_version},<{upper_bound}"
            violations.append(
                f"{distribution}: 内部依赖 {requirement.name} 必须使用 {expected}，"
                f"实际为 {raw_requirement!r}"
            )


def validate_release(
    repository: Path,
    *,
    expected_version: str | None = None,
    tag: str | None = None,
) -> tuple[str, ...]:
    """校验一次同步发布所需的全部契约。

    Args:
        repository: MatterLoop 工作区根目录。
        expected_version: 调用方期望发布的 ``X.Y.Z`` 版本。
        tag: Git 发布标签，必须使用 ``vX.Y.Z`` 格式。

    Returns:
        按发现顺序排列的中文违规说明；空元组表示校验通过。
    """
    repository = repository.resolve()
    violations: list[str] = []
    _validate_workspace(repository, violations)

    parsed_expected = (
        _parse_release_version(expected_version, "--expected-version", violations)
        if expected_version is not None
        else None
    )
    parsed_tag: Version | None = None
    if tag is not None:
        match = RELEASE_TAG_PATTERN.fullmatch(tag)
        if match is None:
            violations.append(f"--tag 必须使用 vX.Y.Z 格式，实际为 {tag!r}")
        else:
            parsed_tag = _parse_release_version(match.group("version"), "--tag", violations)

    package_data: dict[str, tuple[Mapping[str, object], Mapping[str, object], Version]] = {}
    for distribution in DISTRIBUTIONS:
        pyproject_path = repository / distribution / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        try:
            pyproject = _read_pyproject(pyproject_path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            violations.append(f"{distribution}: 无法读取 pyproject.toml：{exc}")
            continue
        project = _mapping(pyproject.get("project"))
        raw_version = project.get("version")
        if not isinstance(raw_version, str):
            violations.append(f"{distribution}: project.version 必须是字符串")
            continue
        version = _parse_release_version(
            raw_version, f"{distribution}: project.version", violations
        )
        if version is not None:
            package_data[distribution] = (pyproject, project, version)

    versions = {version for _, _, version in package_data.values()}
    release_version: Version | None = next(iter(versions)) if len(versions) == 1 else None
    if len(package_data) == len(DISTRIBUTIONS) and len(versions) > 1:
        details = "，".join(
            f"{distribution}={version}" for distribution, (_, _, version) in package_data.items()
        )
        violations.append(f"十二个发行包版本不一致：{details}")

    if parsed_expected is not None and parsed_tag is not None and parsed_expected != parsed_tag:
        violations.append(f"--expected-version {parsed_expected} 与 --tag v{parsed_tag} 不一致")
    if release_version is not None:
        if parsed_expected is not None and parsed_expected != release_version:
            violations.append(
                f"发布版本 {release_version} 与 --expected-version {parsed_expected} 不一致"
            )
        if parsed_tag is not None and parsed_tag != release_version:
            violations.append(f"发布版本 {release_version} 与 --tag v{parsed_tag} 不一致")

    for distribution, (pyproject, project, version) in package_data.items():
        package_root = repository / distribution
        _validate_materials(repository, package_root, project, distribution, violations)
        _validate_type_markers(package_root, pyproject, distribution, violations)
        _validate_internal_requirements(project, distribution, version, violations)

    return tuple(violations)


def main(arguments: Sequence[str] | None = None) -> int:
    """执行发布契约校验并返回适合 CI 使用的退出码。"""
    parser = argparse.ArgumentParser(description="校验 MatterLoop 十二个发行包的发布契约")
    parser.add_argument("--expected-version", help="期望发布的版本，格式为 X.Y.Z")
    parser.add_argument("--tag", help="Git 发布标签，格式为 vX.Y.Z")
    options = parser.parse_args(arguments)

    violations = validate_release(
        REPOSITORY_ROOT,
        expected_version=options.expected_version,
        tag=options.tag,
    )
    if violations:
        print("发布前校验失败：", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1

    version = options.expected_version or (options.tag[1:] if options.tag else "工作区当前版本")
    print(f"发布前校验通过：12 个发行包统一为 {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
