"""统一能力契约与本地工具装饰器测试。"""

from collections.abc import Mapping

import pytest
from matterloop_tools import (
    MCP_CAPABILITY_ANNOTATION,
    CapabilitySpec,
    ToolContext,
    ToolEffect,
    ToolOrigin,
    ToolRegistry,
    capability,
    capability_from_annotations,
    mcp_tool,
    tool,
)


def _submission_capability() -> CapabilitySpec:
    return CapabilitySpec(
        capability_id="polymer.job.submit",
        operations=("submit",),
        entities=("polymer_job",),
        result_kind="job_receipt",
        required_result_fields=("job_id",),
        truthy_result_fields=("submitted",),
    )


async def test_local_tool_decorator_registers_and_invokes_structured_result() -> None:
    @tool(
        description="提交高分子任务",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        default_effect=ToolEffect.WRITE,
        capability_spec=_submission_capability(),
    )
    async def submit_polymer(name: str) -> Mapping[str, object]:
        return {"submitted": True, "job_id": f"job-{name}"}

    registry = ToolRegistry([submit_polymer])
    selected = registry.select_by_capability(
        operation="submit",
        entity="polymer_job",
    )
    result = await registry.invoke(
        "submit_polymer",
        {"name": "pe"},
        context=ToolContext("run-1"),
    )

    assert selected[0].origin is ToolOrigin.LOCAL
    assert selected[0].capability == _submission_capability()
    assert result.structured_content == {"submitted": True, "job_id": "job-pe"}
    assert selected[0].capability is not None
    assert selected[0].capability.evidence_satisfied(result.structured_content)


async def test_capability_decorator_composes_with_tool_decorator() -> None:
    @tool(description="查询任务", input_schema={"type": "object"})
    @capability(
        CapabilitySpec(
            capability_id="polymer.job.query",
            operations=("query_status",),
            entities=("polymer_job",),
        )
    )
    def query_job() -> Mapping[str, object]:
        return {"status": "running"}

    descriptor = query_job.descriptor

    assert descriptor.capability is not None
    assert descriptor.capability.capability_id == "polymer.job.query"


def test_mcp_tool_decorator_exports_namespaced_capability_annotations() -> None:
    spec = _submission_capability()

    @mcp_tool(spec)
    def submit_polymer_job() -> None:
        return None

    annotations = submit_polymer_job.__matterloop_mcp_annotations__

    assert submit_polymer_job.__matterloop_origin__ is ToolOrigin.LOCAL_MCP
    assert annotations[MCP_CAPABILITY_ANNOTATION]["capability_id"] == "polymer.job.submit"


def test_completion_evidence_rejects_missing_or_false_receipt() -> None:
    spec = _submission_capability()

    assert not spec.evidence_satisfied({"submitted": True})
    assert not spec.evidence_satisfied({"submitted": False, "job_id": "job-1"})
    assert spec.evidence_satisfied({"submitted": True, "job_id": "job-1"})


def test_capability_rejects_empty_identifiers() -> None:
    with pytest.raises(ValueError):
        CapabilitySpec(capability_id=" ")


def test_malformed_mcp_capability_annotation_is_not_selected() -> None:
    capability_spec = capability_from_annotations({
        MCP_CAPABILITY_ANNOTATION: {
            "capability_id": "polymer.job.submit",
            "operations": [None],
        }
    })

    assert capability_spec is None
