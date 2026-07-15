"""Skill 受控上下文与只读 Tool 适配器测试。"""

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from matterloop_tools import ToolContext, ToolInputError, ToolRegistry
from matterloop_tools.skills import (
    SkillAccessDeniedError,
    SkillAccessPolicy,
    SkillConfigurationError,
    SkillContent,
    SkillContentTrust,
    SkillContextAdapter,
    SkillReferenceTool,
    SkillRegistry,
    SkillSpec,
    SkillTool,
    SkillTooLargeError,
)


def _content(name: str, markdown: str) -> SkillContent:
    return SkillContent(
        spec=SkillSpec(name, f"{name} description", f"{name}/SKILL.md", version="1.0"),
        markdown=markdown,
        sha256=hashlib.sha256(markdown.encode()).hexdigest(),
    )


def test_context_adapter_filters_discovery_and_marks_content_untrusted() -> None:
    registry = SkillRegistry([_content("allowed", "reference"), _content("hidden", "secret")])
    policy = SkillAccessPolicy.from_names(["allowed"])
    adapter = SkillContextAdapter(registry, policy)

    assert tuple(spec.name for spec in adapter.discover()) == ("allowed",)
    block = adapter.get_context("allowed")
    assert block.content == "reference"
    assert block.trust is SkillContentTrust.UNTRUSTED_REFERENCE
    with pytest.raises(FrozenInstanceError):
        block.content = "changed"  # type: ignore[misc]
    with pytest.raises(SkillAccessDeniedError):
        adapter.get_context("hidden")


def test_context_adapter_enforces_injection_character_limit() -> None:
    adapter = SkillContextAdapter(
        SkillRegistry([_content("large", "12345")]),
        SkillAccessPolicy.from_names(["large"], max_content_chars=4),
    )

    with pytest.raises(SkillTooLargeError, match="max_content_chars"):
        adapter.get_context("large")


def test_access_policy_requires_a_strictly_positive_integer_limit() -> None:
    with pytest.raises(SkillConfigurationError, match="positive integer"):
        SkillAccessPolicy.from_names([], max_content_chars=True)
    with pytest.raises(SkillConfigurationError, match="positive integer"):
        SkillAccessPolicy.from_names([], max_content_chars=0)
    with pytest.raises(SkillConfigurationError, match="positive integer"):
        SkillAccessPolicy.from_names(
            [],
            max_content_chars=1.5,  # type: ignore[arg-type]
        )


async def test_reference_tool_exposes_data_without_executing_skill_content(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"
    command = f"touch {marker}"
    adapter = SkillContextAdapter(
        SkillRegistry([_content("unsafe-text", command)]),
        SkillAccessPolicy.from_names(["unsafe-text"]),
    )
    tools = ToolRegistry([SkillTool(adapter)])
    context = ToolContext("run-1")

    catalog_result = await tools.invoke(
        "skill_reference",
        {"operation": "list"},
        context=context,
    )
    reference_result = await tools.invoke(
        "skill_reference",
        {"operation": "get", "name": "unsafe-text"},
        context=context,
    )

    catalog = json.loads(catalog_result.content)
    reference = json.loads(reference_result.content)
    assert catalog["kind"] == "skill_catalog"
    assert catalog["skills"][0]["name"] == "unsafe-text"
    assert reference["kind"] == "untrusted_reference"
    assert reference["content"] == command
    assert reference_result.metadata["trust"] == "untrusted_reference"
    assert not marker.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"operation": "execute", "name": "allowed"},
        {"operation": "get"},
        {"operation": "get", "name": "../outside"},
        {"operation": "list", "name": "unexpected"},
    ],
)
async def test_reference_tool_rejects_unsupported_operations_or_arguments(
    arguments: dict[str, object],
) -> None:
    adapter = SkillContextAdapter(
        SkillRegistry([_content("allowed", "body")]),
        SkillAccessPolicy.from_names(["allowed"]),
    )
    tool = SkillReferenceTool(adapter)

    with pytest.raises(ToolInputError):
        await tool.invoke(arguments, ToolContext("run-1"))


def test_reference_tool_is_a_semantic_alias() -> None:
    assert SkillReferenceTool is SkillTool
