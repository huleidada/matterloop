"""官方 MCP SDK v1 DTO 的可选兼容性测试。"""

from __future__ import annotations

import pytest
from matterloop_tools.mcp import StructuralMcpResponseMapper

types = pytest.importorskip("mcp.types", reason="optional MCP SDK extra is not installed")


def test_mapper_accepts_official_v1_resources_prompts_and_capabilities() -> None:
    """真实 Pydantic DTO 的 AnyUrl 与可空字段必须保持完整语义。"""
    mapper = StructuralMcpResponseMapper()
    text = types.TextResourceContents(
        uri="memory://guide",
        text="企业指南",
        mimeType="text/plain",
    )
    blob = types.BlobResourceContents(uri="memory://blob", blob="YmluYXJ5")

    resource_page = mapper.map_resource_page(
        types.ListResourcesResult(resources=[types.Resource(uri="memory://guide", name="guide")])
    )
    resource_result = mapper.map_resource_result(types.ReadResourceResult(contents=[text, blob]))
    prompt_page = mapper.map_prompt_page(
        types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name="summarize",
                    arguments=[types.PromptArgument(name="topic")],
                )
            ]
        )
    )
    call_result = mapper.map_call_result(
        types.CallToolResult(
            content=[
                types.ResourceLink(
                    type="resource_link",
                    uri="memory://linked",
                    name="linked",
                ),
                types.EmbeddedResource(type="resource", resource=text),
            ]
        )
    )
    capabilities = mapper.map_capabilities(
        types.InitializeResult(
            protocolVersion="2025-11-25",
            capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=True)),
            serverInfo=types.Implementation(name="test-server", version="1.0.0"),
        )
    )

    assert resource_page.items[0].uri == "memory://guide"
    assert resource_result.contents[0].text == "企业指南"
    assert resource_result.contents[0].uri == "memory://guide"
    assert resource_result.contents[1].data == "YmluYXJ5"
    assert prompt_page.items[0].arguments[0].required is False
    assert call_result.content[0].uri == "memory://linked"
    assert call_result.content[1].text == "企业指南"
    assert capabilities.tools is True
    assert capabilities.resources is False
    assert capabilities.prompts is False
