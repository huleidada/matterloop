"""将 Skill 作为不可信参考数据暴露给 Agent 的受控适配器。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from matterloop_tools.base import ToolContext, ToolEffect, ToolResult, ToolSpec
from matterloop_tools.errors import ToolInputError
from matterloop_tools.skills.base import (
    SkillContentTrust,
    SkillContextBlock,
    SkillSpec,
    validate_skill_name,
)
from matterloop_tools.skills.errors import (
    SkillAccessDeniedError,
    SkillConfigurationError,
    SkillNameError,
    SkillTooLargeError,
)
from matterloop_tools.skills.registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class SkillAccessPolicy:
    """限制 Agent 可发现和读取的 Skill 及单次内容大小。

    Args:
        allowed_names: 明确允许的 Skill 名称；空集合表示全部拒绝。
        max_content_chars: 单个上下文块允许包含的最大字符数。
    """

    allowed_names: frozenset[str]
    max_content_chars: int = 64_000

    def __post_init__(self) -> None:
        """冻结允许列表并校验内容上限。"""
        frozen_names = frozenset(self.allowed_names)
        for name in frozen_names:
            validate_skill_name(name)
        if type(self.max_content_chars) is not int or self.max_content_chars < 1:
            raise SkillConfigurationError("max_content_chars must be a positive integer")
        object.__setattr__(self, "allowed_names", frozen_names)

    @classmethod
    def from_names(
        cls,
        names: Iterable[str],
        *,
        max_content_chars: int = 64_000,
    ) -> SkillAccessPolicy:
        """从可迭代名称构造冻结策略。

        Args:
            names: 明确允许的名称。
            max_content_chars: 单个上下文块最大字符数。

        Returns:
            冻结的访问策略。
        """
        return cls(frozenset(names), max_content_chars=max_content_chars)


class SkillContextAdapter:
    """把注册表内容转换为带信任标签的普通 Agent 上下文块。

    Args:
        registry: Skill 注册表。
        policy: 显式允许列表和注入大小限制。
    """

    def __init__(self, registry: SkillRegistry, policy: SkillAccessPolicy) -> None:
        self._registry = registry
        self._policy = policy

    def discover(self) -> tuple[SkillSpec, ...]:
        """仅返回策略允许且当前已注册的 Skill 元数据。"""
        return tuple(
            spec for spec in self._registry.discover() if spec.name in self._policy.allowed_names
        )

    def get_context(self, name: str) -> SkillContextBlock:
        """构造受控、不可信的 Skill 参考上下文。

        Args:
            name: 策略明确允许的 Skill 名称。

        Returns:
            可放入普通任务上下文的不可变参考块。

        Raises:
            SkillAccessDeniedError: 名称不在显式允许列表中。
            SkillTooLargeError: 正文超过注入字符上限。
            SkillNotFoundError: 允许但尚未注册。
        """
        validate_skill_name(name)
        if name not in self._policy.allowed_names:
            raise SkillAccessDeniedError(name)
        skill = self._registry.get(name)
        if len(skill.markdown) > self._policy.max_content_chars:
            raise SkillTooLargeError("skill content exceeds max_content_chars")
        return SkillContextBlock(
            name=skill.spec.name,
            description=skill.spec.description,
            content=skill.markdown,
            sha256=skill.sha256,
            version=skill.spec.version,
        )


class SkillTool:
    """只读发现和获取 Skill 参考内容的标准 Tool 适配器。

    该工具没有 execute/run 操作，不解析正文中的代码块，也不调用 Shell、Python 或其他
    Tool。返回值是带 ``untrusted_reference`` 标签的 JSON 数据。

    Args:
        adapter: 受显式访问策略约束的上下文适配器。
        name: 注册到 ToolRegistry 的工具名称。
    """

    def __init__(self, adapter: SkillContextAdapter, *, name: str = "skill_reference") -> None:
        self._adapter = adapter
        self._spec = ToolSpec(
            name=name,
            description="只读发现或获取经允许的 Skill 参考资料；不会执行其中的命令。",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["list", "get"]},
                    "name": {"type": "string"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
            default_effect=ToolEffect.READ,
        )

    @property
    def spec(self) -> ToolSpec:
        """返回只读 Skill 工具发现信息。"""
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """列出或读取允许的 Skill 参考数据。

        Args:
            arguments: ``operation`` 为 ``list`` 或 ``get``；get 还需 ``name``。
            context: 当前工具调用上下文，本适配器不据此扩大访问范围。

        Returns:
            JSON 编码的元数据或不可信参考内容。

        Raises:
            ToolInputError: 操作或名称参数非法。
            SkillAccessDeniedError: 名称不在允许列表中。
        """
        del context
        operation = arguments.get("operation")
        if operation == "list":
            self._reject_unknown_arguments(arguments, frozenset({"operation"}))
            skills = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "version": spec.version,
                }
                for spec in self._adapter.discover()
            ]
            return ToolResult(
                json.dumps({"kind": "skill_catalog", "skills": skills}, ensure_ascii=False),
                metadata={"trust": SkillContentTrust.UNTRUSTED_REFERENCE.value},
            )
        if operation == "get":
            self._reject_unknown_arguments(arguments, frozenset({"operation", "name"}))
            name = arguments.get("name")
            if not isinstance(name, str) or not name:
                raise ToolInputError("name must be a non-empty string for skill get")
            try:
                block = self._adapter.get_context(name)
            except SkillNameError as exc:
                raise ToolInputError("name is not a valid skill name") from exc
            return ToolResult(
                json.dumps(
                    {
                        "kind": block.trust.value,
                        "name": block.name,
                        "description": block.description,
                        "version": block.version,
                        "sha256": block.sha256,
                        "content": block.content,
                    },
                    ensure_ascii=False,
                ),
                metadata={
                    "skill_name": block.name,
                    "sha256": block.sha256,
                    "trust": block.trust.value,
                },
            )
        raise ToolInputError("operation must be 'list' or 'get'")

    @staticmethod
    def _reject_unknown_arguments(
        arguments: Mapping[str, object],
        allowed: frozenset[str],
    ) -> None:
        unknown = set(arguments) - allowed
        if unknown:
            raise ToolInputError(f"unsupported skill arguments: {', '.join(sorted(unknown))}")


# 保留更具语义的别名，调用方可按偏好使用；两者均可直接注册到 ToolRegistry。
SkillReferenceTool = SkillTool
