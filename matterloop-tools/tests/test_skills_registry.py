"""Skill 注册、热替换和原子刷新测试。"""

import hashlib
from pathlib import Path

import pytest
from matterloop_tools.skills import (
    SkillContent,
    SkillExistsError,
    SkillFormatError,
    SkillLoader,
    SkillLoaderConfig,
    SkillNotFoundError,
    SkillRegistry,
    SkillSpec,
)


def _content(name: str, markdown: str) -> SkillContent:
    return SkillContent(
        spec=SkillSpec(name, f"{name} description", f"{name}/SKILL.md"),
        markdown=markdown,
        sha256=hashlib.sha256(markdown.encode()).hexdigest(),
    )


def _write_skill(root: Path, name: str, body: str) -> None:
    directory = root / name
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n{body}",
        encoding="utf-8",
    )


def test_registry_registers_discovers_replaces_and_unregisters() -> None:
    old = _content("review", "old")
    new = _content("review", "new")
    registry = SkillRegistry([old])

    assert registry.names() == ("review",)
    assert registry.discover() == (old.spec,)
    assert registry.get("review") is old
    with pytest.raises(SkillExistsError):
        registry.register(new)

    registry.replace("review", new)

    assert registry.get("review") is new
    assert old.markdown == "old"
    registry.unregister("review")
    with pytest.raises(SkillNotFoundError):
        registry.get("review")


def test_registry_replacement_requires_existing_matching_name() -> None:
    registry = SkillRegistry([_content("alpha", "body")])

    with pytest.raises(ValueError, match="must match"):
        registry.replace("alpha", _content("beta", "body"))
    with pytest.raises(SkillNotFoundError):
        registry.replace("missing", _content("missing", "body"))


def test_registry_refresh_replaces_complete_snapshot(tmp_path: Path) -> None:
    registry = SkillRegistry([_content("old", "old body")])
    _write_skill(tmp_path, "alpha", "alpha body")
    _write_skill(tmp_path, "beta", "beta body")

    specs = registry.refresh(SkillLoader(SkillLoaderConfig(tmp_path)))

    assert tuple(spec.name for spec in specs) == ("alpha", "beta")
    assert registry.names() == ("alpha", "beta")
    with pytest.raises(SkillNotFoundError):
        registry.get("old")


def test_registry_refresh_keeps_old_snapshot_when_any_document_fails(tmp_path: Path) -> None:
    old = _content("old", "stable")
    registry = SkillRegistry([old])
    _write_skill(tmp_path, "alpha", "valid")
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: broken\ndescription: >-\n---\ninvalid",
        encoding="utf-8",
    )

    with pytest.raises(SkillFormatError):
        registry.refresh(SkillLoader(SkillLoaderConfig(tmp_path)))

    assert registry.names() == ("old",)
    assert registry.get("old") is old
