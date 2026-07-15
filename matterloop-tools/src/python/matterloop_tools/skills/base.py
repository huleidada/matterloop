"""Skill 元数据、不可变内容和受控上下文 DTO。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from matterloop_tools.skills.errors import SkillFormatError, SkillNameError

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_SKILL_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[A-Za-z0-9._+-]*[A-Za-z0-9])?$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 500
_MAX_VERSION_LENGTH = 64


def validate_skill_name(name: str) -> str:
    """校验并返回不经规范化的 Skill 名称。

    名称不做大小写或字符替换，避免不同输入被静默映射到同一路径。

    Args:
        name: 待校验名称。

    Returns:
        原始有效名称。

    Raises:
        SkillNameError: 名称为空、过长或含路径及特殊字符。
    """
    if not isinstance(name, str):
        raise SkillNameError("skill name must be a string")
    if not 1 <= len(name) <= _MAX_NAME_LENGTH or not _SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillNameError(
            "skill name must contain 1-64 lowercase letters, digits, hyphens or underscores"
        )
    return name


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """描述一个可发现 Skill 的稳定元数据。

    Args:
        name: 唯一名称，同时对应根目录下的目录名。
        description: 面向调用方的简短用途说明。
        source: 相对 Skill 根目录的来源路径。
        version: 可选的不透明版本标签。
    """

    name: str
    description: str
    source: str
    version: str | None = None

    def __post_init__(self) -> None:
        """校验名称、说明、版本和相对来源路径。"""
        validate_skill_name(self.name)
        if not self.description.strip() or len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise SkillFormatError("skill description must contain 1-500 characters")
        source = PurePosixPath(self.source)
        if source.is_absolute() or ".." in source.parts or source.parts != (self.name, "SKILL.md"):
            raise SkillFormatError("skill source must be '<name>/SKILL.md'")
        if self.version is not None and (
            not 1 <= len(self.version) <= _MAX_VERSION_LENGTH
            or not _SKILL_VERSION_PATTERN.fullmatch(self.version)
        ):
            raise SkillFormatError("skill version contains unsupported characters")


@dataclass(frozen=True, slots=True)
class SkillContent:
    """保存经过安全加载的不可变 Skill Markdown 内容。

    Args:
        spec: Skill 发现元数据。
        markdown: 去除可选 frontmatter 后的 Markdown 正文。
        sha256: ``markdown`` UTF-8 字节的 SHA-256 十六进制摘要。
    """

    spec: SkillSpec
    markdown: str
    sha256: str

    def __post_init__(self) -> None:
        """拒绝空正文、非法摘要和与正文不一致的摘要。"""
        if not self.markdown.strip():
            raise SkillFormatError("skill markdown must not be empty")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise SkillFormatError("skill sha256 must be a lowercase SHA-256 digest")
        expected_sha256 = hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()
        if self.sha256 != expected_sha256:
            raise SkillFormatError("skill sha256 must match the markdown content")


class SkillContentTrust(str, Enum):
    """标识 Skill 内容在 Agent 上下文中的信任等级。"""

    UNTRUSTED_REFERENCE = "untrusted_reference"


@dataclass(frozen=True, slots=True)
class SkillContextBlock:
    """允许调用方注入普通 Agent 上下文的受控参考块。

    该 DTO 明确把 Skill 标记为不可信参考数据。宿主只能将其放入普通用户或任务上下文，
    不应提升为 system/developer 消息，也不应把正文解释为待执行命令。

    Args:
        name: Skill 名称。
        description: Skill 用途说明。
        content: 有大小上限的 Markdown 参考内容。
        sha256: ``content`` UTF-8 字节的摘要，用于审计和缓存校验。
        trust: 固定的不可信参考等级。
        version: 可选版本标签。
    """

    name: str
    description: str
    content: str
    sha256: str
    trust: SkillContentTrust = SkillContentTrust.UNTRUSTED_REFERENCE
    version: str | None = None

    def __post_init__(self) -> None:
        """校验上下文块没有绕过 Skill 基础字段约束。"""
        validate_skill_name(self.name)
        if not self.description.strip() or len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise SkillFormatError("skill context description must contain 1-500 characters")
        if not self.content.strip():
            raise SkillFormatError("skill context content must not be empty")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise SkillFormatError("skill context sha256 must be a lowercase SHA-256 digest")
        expected_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.sha256 != expected_sha256:
            raise SkillFormatError("skill context sha256 must match the content")
        if self.trust is not SkillContentTrust.UNTRUSTED_REFERENCE:
            raise SkillFormatError("skill context trust must be untrusted_reference")
        if self.version is not None and (
            not 1 <= len(self.version) <= _MAX_VERSION_LENGTH
            or not _SKILL_VERSION_PATTERN.fullmatch(self.version)
        ):
            raise SkillFormatError("skill context version contains unsupported characters")
