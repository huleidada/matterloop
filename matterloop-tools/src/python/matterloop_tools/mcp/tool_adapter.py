"""将 MCP 工具安全适配为 MatterLoop Tool。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from typing import Protocol

from matterloop_tools.base import ToolContext, ToolEffect, ToolResult, ToolSpec
from matterloop_tools.mcp.models import McpCallResult, McpContent, McpContentKind, McpToolDefinition

_UNSAFE_TOOL_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_TOOL_NAME_LENGTH = 64
_JSON_STRING_CHUNK_CHARACTERS = 1_024
_UNSERIALIZABLE_CONTENT = "[unserializable MCP structured content]"


class _BoundedTextBuilder:
    """在固定字符预算内增量构造结果文本。"""

    __slots__ = ("_length", "_limit", "_parts", "truncated")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._length = 0
        self._parts: list[str] = []
        self.truncated = False

    @property
    def has_content(self) -> bool:
        """返回是否已经写入至少一个字符。"""
        return self._length > 0

    @property
    def remaining(self) -> int:
        """返回尚可写入的字符数。"""
        return self._limit - self._length

    def append(self, value: str) -> None:
        """只复制预算内的前缀，并记录是否发生截断。"""
        if not value or self.truncated:
            return
        remaining = self.remaining
        if len(value) > remaining:
            if remaining:
                self._parts.append(value[:remaining])
                self._length += remaining
            self.truncated = True
            return
        self._parts.append(value)
        self._length += len(value)

    def mark_truncated(self) -> None:
        """标记上游增量编码器仍有未写入内容。"""
        self.truncated = True

    def build(self) -> str:
        """合并不超过字符上限的小片段。"""
        return "".join(self._parts)


class McpToolCaller(Protocol):
    """MCP Tool Adapter 所需的最小注册表调用面。"""

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        catalog_token: str | None = None,
    ) -> McpCallResult:
        """在服务连接租约内调用远端工具。"""
        ...


def safe_mcp_tool_name(namespace: str, remote_name: str) -> str:
    """生成符合模型工具命名约束的 MCP 安全名称。

    Args:
        namespace: 由组合根为服务分配的稳定命名空间。
        remote_name: MCP 服务声明的原始工具名。

    Returns:
        仅包含字母、数字、下划线和连字符且不超过 64 字符的名称。

    Raises:
        ValueError: 命名空间或远端名称规范化后为空。
    """
    safe_namespace = _safe_segment(namespace)
    safe_remote_name = _safe_segment(remote_name)
    exposed_name = f"mcp__{safe_namespace}__{safe_remote_name}"
    if len(exposed_name) <= _MAX_TOOL_NAME_LENGTH:
        return exposed_name
    digest = hashlib.sha256(f"{namespace}\0{remote_name}".encode()).hexdigest()[:10]
    prefix = f"mcp__{safe_namespace[:20]}__"
    available = _MAX_TOOL_NAME_LENGTH - len(prefix) - len(digest) - 1
    return f"{prefix}{safe_remote_name[:available]}_{digest}"


class McpToolAdapter:
    """通过 MCP 服务注册表调用远端工具的 MatterLoop Tool。

    Adapter 不长期持有具体 Session。注册表发现得到的 Adapter 会绑定连接目录令牌；连接热替换
    后旧 Adapter 拒绝新调用，宿主必须重新发现并原子替换工具，避免旧 Schema 调用新实现。
    """

    def __init__(
        self,
        caller: McpToolCaller,
        server_name: str,
        namespace: str,
        definition: McpToolDefinition,
        *,
        max_result_characters: int,
        max_content_blocks: int = 256,
        catalog_token: str | None = None,
    ) -> None:
        """初始化 MCP 工具适配器。

        Args:
            caller: 提供连接租约的 MCP 调用入口。
            server_name: MCP 服务注册名称。
            namespace: 暴露给模型的安全工具命名空间。
            definition: 发现阶段返回的远端工具定义。
            max_result_characters: 标准文本结果的字符硬上限。
            max_content_blocks: 单次结果最多渲染的内容块数量。
            catalog_token: 可选连接目录令牌；注册表发现时自动提供。
        """
        if type(max_result_characters) is not int or max_result_characters <= 0:
            raise ValueError("max_result_characters must be a positive integer")
        if type(max_content_blocks) is not int or max_content_blocks <= 0:
            raise ValueError("max_content_blocks must be a positive integer")
        if catalog_token is not None and (not isinstance(catalog_token, str) or not catalog_token):
            raise ValueError("catalog_token must be a non-empty string when provided")
        self._caller = caller
        self._server_name = server_name
        self._remote_name = definition.name
        self._max_result_characters = max_result_characters
        self._max_content_blocks = max_content_blocks
        self._catalog_token = catalog_token
        description = definition.description.strip() or (
            f"调用 MCP 服务 {server_name} 的 {definition.name} 工具。"
        )
        self._spec = ToolSpec(
            safe_mcp_tool_name(namespace, definition.name),
            description,
            definition.input_schema,
            default_effect=ToolEffect.UNKNOWN,
        )

    @property
    def spec(self) -> ToolSpec:
        """返回带安全命名空间的 MatterLoop 工具定义。"""
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """在当前 MCP 连接租约内调用远端工具。

        Args:
            arguments: 经过 Agent 或调用方构造的结构化参数。
            context: MatterLoop 工具调用上下文；不会自动发送给 MCP 服务。

        Returns:
            内容和错误标志均已标准化的 ToolResult。
        """
        del context
        result = await self._caller.call_tool(
            self._server_name,
            self._remote_name,
            arguments,
            catalog_token=self._catalog_token,
        )
        content, truncated = self._render_result(
            result,
            max_characters=self._max_result_characters,
            max_content_blocks=self._max_content_blocks,
        )
        return ToolResult(
            content,
            is_error=result.is_error,
            metadata={
                "mcp_server": self._server_name,
                "mcp_tool": self._remote_name,
                "content_blocks": len(result.content),
                "truncated": truncated,
            },
        )

    @classmethod
    def _render_result(
        cls,
        result: McpCallResult,
        *,
        max_characters: int,
        max_content_blocks: int,
    ) -> tuple[str, bool]:
        """按字符预算增量渲染 MCP 结果，避免复制完整远端载荷。"""
        builder = _BoundedTextBuilder(max_characters)
        for index, item in enumerate(result.content):
            if index >= max_content_blocks:
                builder.mark_truncated()
                break
            cls._append_content(builder, item)
            if builder.truncated:
                break
        if result.structured_content:
            cls._append_json_block(builder, result.structured_content)
        return builder.build(), builder.truncated

    @classmethod
    def _append_content(cls, builder: _BoundedTextBuilder, content: McpContent) -> None:
        """将一个标准化内容块追加到有界结果。"""
        if content.kind is McpContentKind.TEXT:
            cls._append_text_block(builder, content.text or "")
            return
        if content.kind is McpContentKind.JSON:
            cls._append_json_block(builder, content.data)
            return
        if content.kind is McpContentKind.RESOURCE:
            if content.text is not None:
                cls._append_text_block(builder, content.text)
                return
            cls._append_text_block(builder, f"[MCP resource: {content.uri or 'embedded'}]")
            return
        if content.kind in {McpContentKind.IMAGE, McpContentKind.AUDIO, McpContentKind.BINARY}:
            cls._append_text_block(
                builder,
                f"[MCP {content.kind.value} content: {content.mime_type or 'unknown'}]",
            )
            return
        cls._append_text_block(builder, "[unsupported MCP content]")

    @staticmethod
    def _append_text_block(builder: _BoundedTextBuilder, value: str) -> None:
        """追加非空文本块，并保持块间换行语义。"""
        if not value or builder.truncated:
            return
        if builder.has_content:
            builder.append("\n")
            if builder.truncated:
                return
        builder.append(value)

    @classmethod
    def _append_json_block(cls, builder: _BoundedTextBuilder, value: object) -> None:
        """在剩余预算内增量编码并追加一个 JSON 块。"""
        if builder.truncated:
            return
        if builder.has_content:
            builder.append("\n")
            if builder.truncated:
                return
        rendered, truncated = cls._json_text(value, max_characters=builder.remaining)
        builder.append(rendered)
        if truncated:
            builder.mark_truncated()

    @staticmethod
    def _json_text(value: object, *, max_characters: int) -> tuple[str, bool]:
        """以有界小片段编码 JSON，序列化失败时返回稳定占位符。"""
        if max_characters == 0:
            return "", True
        try:
            return _collect_bounded_chunks(
                _iter_json_chunks(value, active_container_ids=set()),
                max_characters=max_characters,
            )
        except (OverflowError, RecursionError, TypeError, ValueError):
            return _bounded_text(_UNSERIALIZABLE_CONTENT, max_characters=max_characters)


def _safe_segment(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("MCP tool namespace and name must not be empty")
    normalized = _UNSAFE_TOOL_CHARACTERS.sub("_", stripped).strip("_")
    if not normalized:
        # 全中文等合法远端名称没有 ASCII 片段时仍应可发现；摘要保持确定且不泄漏内容。
        normalized = f"u_{hashlib.sha256(stripped.encode()).hexdigest()[:12]}"
    return normalized


def _iter_json_chunks(value: object, *, active_container_ids: set[int]) -> Iterator[str]:
    """按 JSON 语法顺序生成有界片段，不构造完整序列化字符串。"""
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_container_ids:
            raise ValueError("circular MCP structured content")
        active_container_ids.add(identity)
        try:
            yield "{"
            for index, (key, item) in enumerate(value.items()):
                if not isinstance(key, str):
                    raise TypeError("MCP structured content keys must be strings")
                if index:
                    yield ","
                yield from _iter_json_string_chunks(key)
                yield ":"
                yield from _iter_json_chunks(
                    item,
                    active_container_ids=active_container_ids,
                )
            yield "}"
        finally:
            active_container_ids.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_container_ids:
            raise ValueError("circular MCP structured content")
        active_container_ids.add(identity)
        try:
            yield "["
            for index, item in enumerate(value):
                if index:
                    yield ","
                yield from _iter_json_chunks(item, active_container_ids=active_container_ids)
            yield "]"
        finally:
            active_container_ids.remove(identity)
        return
    if isinstance(value, str):
        yield from _iter_json_string_chunks(value)
        return
    # 标量很小，使用标准编码器保持数字、布尔值和 null 的兼容行为。
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    yield from encoder.iterencode(value)


def _iter_json_string_chunks(value: str) -> Iterator[str]:
    """分段转义超长 JSON 字符串，限制单次中间字符串大小。"""
    yield '"'
    for start in range(0, len(value), _JSON_STRING_CHUNK_CHARACTERS):
        chunk = value[start : start + _JSON_STRING_CHUNK_CHARACTERS]
        # encode_basestring 的结果仅覆盖固定大小切片，不会复制完整远端字符串。
        yield json.encoder.encode_basestring(chunk)[1:-1]
    yield '"'


def _collect_bounded_chunks(
    chunks: Iterator[str],
    *,
    max_characters: int,
) -> tuple[str, bool]:
    """只收集字符预算内的增量编码片段。"""
    parts: list[str] = []
    length = 0
    for chunk in chunks:
        if not chunk:
            continue
        remaining = max_characters - length
        if len(chunk) > remaining:
            if remaining:
                parts.append(chunk[:remaining])
            return "".join(parts), True
        parts.append(chunk)
        length += len(chunk)
    return "".join(parts), False


def _bounded_text(value: str, *, max_characters: int) -> tuple[str, bool]:
    """截取单个稳定占位符并返回截断标志。"""
    if len(value) > max_characters:
        return value[:max_characters], True
    return value, False
