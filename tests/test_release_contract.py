"""发布版本、内部依赖和发行材料的工作区级契约测试。"""

from __future__ import annotations

from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from scripts import check_release

ROOT = Path(__file__).resolve().parents[1]


def _create_workspace(repository: Path, version: str = "0.1.0") -> None:
    """创建不依赖真实源码的最小有效十二包工作区。"""
    license_text = "MIT License\n\nCopyright MatterLoop Contributors\n"
    (repository / "LICENSE").write_text(license_text, encoding="utf-8")
    for distribution in check_release.DISTRIBUTIONS:
        package_root = repository / distribution
        import_root = distribution.replace("-", "_")
        source_root = package_root / "src" / "python" / import_root
        source_root.mkdir(parents=True)
        (package_root / "README.md").write_text(f"# {distribution}\n", encoding="utf-8")
        (package_root / "LICENSE").write_text(license_text, encoding="utf-8")
        (source_root / "py.typed").write_text("\n", encoding="utf-8")
        (package_root / "pyproject.toml").write_text(
            "\n".join(
                (
                    "[build-system]",
                    'requires = ["hatchling>=1.25"]',
                    'build-backend = "hatchling.build"',
                    "",
                    "[project]",
                    f'name = "{distribution}"',
                    f'version = "{version}"',
                    'readme = "README.md"',
                    'license = "MIT"',
                    'license-files = ["LICENSE"]',
                    "dependencies = []",
                    "",
                    "[tool.hatch.build.targets.wheel]",
                    f'packages = ["src/python/{import_root}"]',
                    "",
                )
            ),
            encoding="utf-8",
        )


def _replace(path: Path, old: str, new: str) -> None:
    """替换测试夹具中的唯一文本片段。"""
    content = path.read_text(encoding="utf-8")
    assert content.count(old) == 1
    path.write_text(content.replace(old, new), encoding="utf-8")


def test_repository_satisfies_release_contract() -> None:
    """真实工作区应满足同步公开发布契约。"""
    assert check_release.validate_release(ROOT) == ()


def test_valid_workspace_accepts_matching_expected_version_and_tag(tmp_path: Path) -> None:
    """十二个包一致且发布参数匹配时应通过。"""
    _create_workspace(tmp_path)

    violations = check_release.validate_release(
        tmp_path,
        expected_version="0.1.0",
        tag="v0.1.0",
    )

    assert violations == ()


def test_version_mismatch_and_release_arguments_are_reported(tmp_path: Path) -> None:
    """包版本、期望版本和标签冲突都应产生清晰错误。"""
    _create_workspace(tmp_path)
    pyproject = tmp_path / "matterloop-tools" / "pyproject.toml"
    _replace(pyproject, 'version = "0.1.0"', 'version = "0.1.1"')

    violations = check_release.validate_release(
        tmp_path,
        expected_version="0.1.1",
        tag="v0.1.2",
    )

    assert any("十二个发行包版本不一致" in violation for violation in violations)
    assert any(
        "--expected-version 0.1.1 与 --tag v0.1.2 不一致" in violation for violation in violations
    )


def test_internal_dependency_must_use_synchronized_minor_range(tmp_path: Path) -> None:
    """内部依赖必须使用当前版本到下一个 minor 的半开区间。"""
    _create_workspace(tmp_path)
    pyproject = tmp_path / "matterloop-agents" / "pyproject.toml"
    _replace(
        pyproject,
        "dependencies = []",
        'dependencies = ["matterloop-core>=0.1.0,<0.1.1"]',
    )

    violations = check_release.validate_release(tmp_path)

    assert any(
        "matterloop-agents: 内部依赖 matterloop-core 必须使用 >=0.1.0,<0.2.0" in violation
        for violation in violations
    )


def test_missing_or_inconsistent_release_materials_are_reported(tmp_path: Path) -> None:
    """README、类型标记和许可证缺失或漂移时应阻止发布。"""
    _create_workspace(tmp_path)
    (tmp_path / "matterloop-core" / "README.md").unlink()
    (tmp_path / "matterloop-models" / "src/python/matterloop_models/py.typed").unlink()
    (tmp_path / "matterloop-runtime" / "LICENSE").write_text(
        "different license\n",
        encoding="utf-8",
    )
    policies_pyproject = tmp_path / "matterloop-policies" / "pyproject.toml"
    _replace(policies_pyproject, 'license-files = ["LICENSE"]\n', "")

    violations = check_release.validate_release(tmp_path)

    assert any("matterloop-core: README 文件不存在或为空" in item for item in violations)
    assert any("matterloop-models: wheel 源码包缺少 py.typed" in item for item in violations)
    assert any(
        "matterloop-runtime: 包内 LICENSE 与仓库根 LICENSE 不一致" in item for item in violations
    )
    assert any(
        'matterloop-policies: project.license-files 必须显式包含 "LICENSE"' in item
        for item in violations
    )


def test_main_returns_nonzero_and_prints_chinese_errors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """CLI 校验失败时应写入标准错误并返回非零退出码。"""
    _create_workspace(tmp_path)
    monkeypatch.setattr(check_release, "REPOSITORY_ROOT", tmp_path)

    exit_code = check_release.main(("--tag", "release-0.1.0"))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "发布前校验失败" in captured.err
    assert "--tag 必须使用 vX.Y.Z 格式" in captured.err
