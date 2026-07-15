"""安全 Skill 加载器测试。"""

import hashlib
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from matterloop_tools.skills import (
    SkillConfigurationError,
    SkillContent,
    SkillFormatError,
    SkillLoader,
    SkillLoaderConfig,
    SkillNameError,
    SkillPathError,
    SkillSpec,
    SkillTooLargeError,
)


def _write_skill(root: Path, name: str, content: str) -> Path:
    directory = root / name
    directory.mkdir()
    skill_path = directory / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def test_loader_parses_minimal_frontmatter_and_freezes_config(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "code-review",
        """---
name: code-review
description: "审查 Python: 类型、安全与测试"
version: 1.2.0
---
# Code review

只提供审查参考，不执行命令。
""",
    )
    config = SkillLoaderConfig(tmp_path)

    skill = SkillLoader(config).load("code-review")

    assert skill.spec.name == "code-review"
    assert skill.spec.description == "审查 Python: 类型、安全与测试"
    assert skill.spec.version == "1.2.0"
    assert skill.spec.source == "code-review/SKILL.md"
    assert skill.markdown.startswith("# Code review")
    assert skill.sha256 == hashlib.sha256(skill.markdown.encode("utf-8")).hexdigest()
    with pytest.raises(FrozenInstanceError):
        config.max_skills = 1  # type: ignore[misc]


def test_loader_supports_document_without_frontmatter(tmp_path: Path) -> None:
    _write_skill(tmp_path, "research", "# Research\n\n收集可验证证据并注明来源。\n")

    skill = SkillLoader(SkillLoaderConfig(tmp_path)).load("research")

    assert skill.spec.description == "收集可验证证据并注明来源。"
    assert skill.markdown == "# Research\n\n收集可验证证据并注明来源。"


def test_skill_content_rejects_digest_that_does_not_match_markdown() -> None:
    with pytest.raises(SkillFormatError, match="match the markdown"):
        SkillContent(
            spec=SkillSpec("forged", "forged description", "forged/SKILL.md"),
            markdown="trusted body",
            sha256=hashlib.sha256(b"different body").hexdigest(),
        )


def test_loader_supports_direct_root_constructor(tmp_path: Path) -> None:
    _write_skill(tmp_path, "direct", "# Direct")

    skill = SkillLoader(tmp_path, max_file_bytes=100).load("direct")

    assert skill.spec.name == "direct"


def test_loader_rejects_limits_mixed_with_config_object(tmp_path: Path) -> None:
    with pytest.raises(SkillConfigurationError, match="either"):
        SkillLoader(SkillLoaderConfig(tmp_path), max_skills=1)


@pytest.mark.parametrize(
    "document",
    [
        "---\nname: wrong-name\ndescription: test\n---\nbody",
        "---\nname: valid\ndescription: >-\n---\nbody",
        "---\nname: valid\ndescription: test\nunknown: value\n---\nbody",
        "---\nname: valid\ndescription: test\ndescription: duplicate\n---\nbody",
        "---\nname: valid\ndescription: test\n---\n",
    ],
)
def test_loader_rejects_unsafe_or_invalid_frontmatter(
    tmp_path: Path,
    document: str,
) -> None:
    _write_skill(tmp_path, "valid", document)

    with pytest.raises(SkillFormatError):
        SkillLoader(SkillLoaderConfig(tmp_path)).load("valid")


@pytest.mark.parametrize("name", ["../outside", "/absolute", "UPPER", "a/b", ".hidden"])
def test_loader_rejects_names_that_could_escape_root(tmp_path: Path, name: str) -> None:
    loader = SkillLoader(SkillLoaderConfig(tmp_path))

    with pytest.raises(SkillNameError):
        loader.load(name)


def test_loader_enforces_file_and_discovery_limits(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", "12345")
    _write_skill(tmp_path, "beta", "body")

    with pytest.raises(SkillTooLargeError, match="max_file_bytes"):
        SkillLoader(SkillLoaderConfig(tmp_path, max_file_bytes=4)).load("alpha")
    with pytest.raises(SkillTooLargeError, match="max_skills"):
        SkillLoader(SkillLoaderConfig(tmp_path, max_skills=1)).discover()


def test_loader_bounds_all_root_entries_before_sorting(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"ignored-{index}.txt").write_text("ignored", encoding="utf-8")

    with pytest.raises(SkillTooLargeError, match="max_scan_entries"):
        SkillLoader(SkillLoaderConfig(tmp_path, max_scan_entries=2)).discover()


def test_loader_discovers_only_direct_skill_directories_in_stable_order(tmp_path: Path) -> None:
    _write_skill(tmp_path, "zeta", "# Zeta")
    _write_skill(tmp_path, "alpha", "# Alpha")
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "empty-directory").mkdir()
    nested = tmp_path / "group" / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("not discovered", encoding="utf-8")

    discovered = SkillLoader(SkillLoaderConfig(tmp_path)).discover()

    assert tuple(skill.spec.name for skill in discovered) == ("alpha", "zeta")


def test_loader_rejects_skill_file_symbolic_link(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    directory = tmp_path / "linked"
    directory.mkdir()
    try:
        (directory / "SKILL.md").symlink_to(outside)

        with pytest.raises(SkillPathError, match="symbolic links"):
            SkillLoader(SkillLoaderConfig(tmp_path)).discover()
    finally:
        outside.unlink(missing_ok=True)


def test_loader_rejects_skill_file_hard_link(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-hard-link-source.md"
    outside.write_text("outside", encoding="utf-8")
    directory = tmp_path / "linked"
    directory.mkdir()
    try:
        os.link(outside, directory / "SKILL.md")

        with pytest.raises(SkillPathError, match="hard links"):
            SkillLoader(SkillLoaderConfig(tmp_path)).load("linked")
    finally:
        outside.unlink(missing_ok=True)


def test_loader_detects_path_swap_without_o_nofollow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_path = _write_skill(tmp_path, "racing", "trusted")
    outside = tmp_path / "outside.md"
    outside.write_text("untrusted", encoding="utf-8")
    real_open = os.open
    swapped = False

    def racing_open(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is None and os.fspath(file) == os.fspath(skill_path):
            swapped = True
            skill_path.unlink()
            skill_path.symlink_to(outside)
        if dir_fd is None:
            return real_open(file, flags, mode)
        return real_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(SkillPathError, match="changed while being opened"):
        SkillLoader(SkillLoaderConfig(tmp_path)).load("racing")


def test_loader_rejects_symbolic_link_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked-root"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SkillPathError, match="symbolic links"):
        SkillLoaderConfig(linked)


def test_loader_rejects_non_utf8_and_nul_content(tmp_path: Path) -> None:
    invalid_utf8 = _write_skill(tmp_path, "binary", "temporary")
    invalid_utf8.write_bytes(b"\xff\xfe")
    _write_skill(tmp_path, "nul", "before\x00after")
    loader = SkillLoader(SkillLoaderConfig(tmp_path))

    with pytest.raises(SkillFormatError, match="UTF-8"):
        loader.load("binary")
    with pytest.raises(SkillFormatError, match="NUL"):
        loader.load("nul")


def test_loader_configuration_requires_existing_directory_and_positive_limits(
    tmp_path: Path,
) -> None:
    with pytest.raises(SkillConfigurationError, match="not a directory"):
        SkillLoaderConfig(tmp_path / "missing")
    with pytest.raises(SkillConfigurationError, match="positive"):
        SkillLoaderConfig(tmp_path, max_skills=0)


def test_loader_configuration_rejects_boolean_and_non_integer_limits(tmp_path: Path) -> None:
    with pytest.raises(SkillConfigurationError, match="max_file_bytes"):
        SkillLoaderConfig(tmp_path, max_file_bytes=True)
    with pytest.raises(SkillConfigurationError, match="max_skills"):
        SkillLoaderConfig(tmp_path, max_skills=True)
    with pytest.raises(SkillConfigurationError, match="max_frontmatter_lines"):
        SkillLoaderConfig(tmp_path, max_frontmatter_lines=True)
    with pytest.raises(SkillConfigurationError, match="max_scan_entries"):
        SkillLoaderConfig(tmp_path, max_scan_entries=True)
    with pytest.raises(SkillConfigurationError, match="max_file_bytes"):
        SkillLoaderConfig(tmp_path, max_file_bytes=1.5)  # type: ignore[arg-type]
