"""演示本地 Tool、注入式 MCP 和只读 Skill 的统一离线装配。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace

from matterloop_tools import (
    McpServerConfig,
    McpServerConnection,
    McpServerRegistry,
    PermissionDecision,
    SkillAccessPolicy,
    SkillContent,
    SkillContextAdapter,
    SkillRegistry,
    SkillSpec,
    SkillTool,
    ToolContext,
    ToolRegistry,
)

logger = logging.getLogger(__name__)


class FakeMcpSession:
    """提供 MCP 标准字段的无网络 Session 测试替身。"""

    def __init__(self) -> None:
        self.closed = False

    async def initialize(self) -> object:
        """模拟 MCP 初始化协商。"""
        return {"protocolVersion": "offline-example"}

    async def list_tools(self, *, cursor: str | None = None) -> object:
        """返回一页远端工具定义。"""
        del cursor
        return {
            "tools": [
                {
                    "name": "evidence.lookup",
                    "description": "读取无副作用的离线证据",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"topic": {"type": "string"}},
                        "required": ["topic"],
                        "additionalProperties": False,
                    },
                }
            ]
        }

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        """返回确定性的离线工具结果。"""
        return {
            "content": [{"type": "text", "text": f"{arguments['topic']}：已核验"}],
            "structuredContent": {"tool": name, "source": "offline-mcp"},
        }

    async def list_resources(self, *, cursor: str | None = None) -> object:
        """返回一页资源定义。"""
        del cursor
        return {"resources": [{"uri": "memory://guide", "name": "guide"}]}

    async def read_resource(self, uri: str) -> object:
        """读取内存资源。"""
        return {"contents": [{"type": "text", "text": f"企业指南：{uri}"}]}

    async def list_resource_templates(self, *, cursor: str | None = None) -> object:
        """返回一页资源模板。"""
        del cursor
        return {
            "resourceTemplates": [
                {"uriTemplate": "memory://documents/{document_id}", "name": "document"}
            ]
        }

    async def list_prompts(self, *, cursor: str | None = None) -> object:
        """返回一页 Prompt 定义。"""
        del cursor
        return {
            "prompts": [
                {
                    "name": "summarize",
                    "arguments": [{"name": "topic", "required": True}],
                }
            ]
        }

    async def get_prompt(self, name: str, arguments: Mapping[str, object]) -> object:
        """解析一个离线 Prompt。"""
        return {
            "description": name,
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": f"总结 {arguments['topic']}"},
                }
            ],
        }

    async def aclose(self) -> None:
        """记录 Session 已由注册表关闭。"""
        self.closed = True


class ExampleToolAuthorizer:
    """只允许示例明确登记的 MCP 与 Skill 工具。"""

    def __init__(self, allowed_names: frozenset[str]) -> None:
        self._allowed_names = allowed_names

    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision:
        """根据固定 allowlist 返回授权决策。"""
        del arguments, context
        if tool_name in self._allowed_names:
            return PermissionDecision.ALLOW
        return PermissionDecision.DENY


@dataclass(frozen=True, slots=True)
class McpSkillsToolsExampleResult:
    """离线 MCP、Skill 与 Tool 装配结果摘要。"""

    tool_names: tuple[str, ...]
    resource_uri: str
    resource_text: str
    resource_template: str
    prompt_name: str
    remote_output: str
    skill_names: tuple[str, ...]
    skill_trust: str
    session_closed: bool


async def run_mcp_skills_tools_example() -> McpSkillsToolsExampleResult:
    """运行无网络、无密钥的 MCP、Skill 与 Tool 完整流程。

    Returns:
        只包含能力名称、标准化结果和关闭状态的摘要。
    """
    session = FakeMcpSession()
    mcp_servers = McpServerRegistry()
    await mcp_servers.register(
        McpServerConnection(
            session,
            McpServerConfig(
                name="knowledge",
                tool_namespace="knowledge",
                owns_session=True,
            ),
        )
    )
    catalog = await mcp_servers.catalog("knowledge")
    remote_tools = await mcp_servers.discover_tools("knowledge")

    markdown = "# 证据审查\n\n只把可追溯来源作为验收证据。"
    skill = SkillContent(
        spec=SkillSpec(
            name="evidence-review",
            description="提供离线证据审查规则",
            source="evidence-review/SKILL.md",
            version="1.0.0",
        ),
        markdown=markdown,
        sha256=hashlib.sha256(markdown.encode()).hexdigest(),
    )
    skills = SkillRegistry((skill,))
    skill_tool = SkillTool(
        SkillContextAdapter(
            skills,
            SkillAccessPolicy.from_names({"evidence-review"}),
        )
    )
    allowed_names = frozenset({remote_tools[0].spec.name, skill_tool.spec.name})
    tools = ToolRegistry(
        (*remote_tools, skill_tool),
        authorizer=ExampleToolAuthorizer(allowed_names),
    )
    try:
        remote_result = await tools.invoke(
            remote_tools[0].spec.name,
            {"topic": "MCP"},
            context=ToolContext("enterprise-mcp-example"),
        )
        skill_result = await tools.invoke(
            skill_tool.spec.name,
            {"operation": "list"},
            context=ToolContext("enterprise-mcp-example"),
        )
        skill_payload = json.loads(skill_result.content)
        if not isinstance(skill_payload, Mapping):
            raise RuntimeError("skill catalog must be a JSON object")
        skill_items = skill_payload.get("skills")
        if not isinstance(skill_items, list):
            raise RuntimeError("skill catalog must contain a skills array")
        skill_names: list[str] = []
        for item in skill_items:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise RuntimeError("skill catalog item must contain a string name")
            skill_names.append(item["name"])
        resource = await mcp_servers.read_resource("knowledge", catalog.resources[0].uri)
        prompt = await mcp_servers.get_prompt(
            "knowledge",
            catalog.prompts[0].name,
            {"topic": "MCP"},
        )
        result = McpSkillsToolsExampleResult(
            tool_names=tools.names(),
            resource_uri=catalog.resources[0].uri,
            resource_text=resource.contents[0].text or "",
            resource_template=catalog.resource_templates[0].uri_template,
            prompt_name=prompt.description,
            remote_output=remote_result.content,
            skill_names=tuple(skill_names),
            skill_trust=str(skill_result.metadata["trust"]),
            session_closed=False,
        )
    finally:
        await tools.aclose()
        await mcp_servers.aclose()
    return replace(result, session_closed=session.closed)


def main() -> None:
    """运行示例并输出不含内容正文和凭据的摘要。"""
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_mcp_skills_tools_example())
    logger.info(
        "MCP、Skill 与 Tool 离线示例完成",
        extra={
            "tool_names": result.tool_names,
            "skill_names": result.skill_names,
            "resource_uri": result.resource_uri,
            "session_closed": result.session_closed,
        },
    )


if __name__ == "__main__":
    main()
