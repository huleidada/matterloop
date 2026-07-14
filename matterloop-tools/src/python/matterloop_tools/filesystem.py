"""受根目录约束的异步文件系统工具。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from matterloop_tools.base import ToolContext, ToolResult, ToolSpec
from matterloop_tools.errors import ToolConfigurationError, ToolInputError


class FileSystemTool:
    """提供默认只读、路径受限的文件访问。

    Args:
        root: 所有访问必须位于其中的根目录。
        allow_write: 是否开放原子文本写入；默认关闭。
        max_read_bytes: 单次读取的最大字节数。
        max_write_bytes: 单次写入的最大字节数。
        max_list_entries: 单次列目录的最大条目数。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        allow_write: bool = False,
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 1_000_000,
        max_list_entries: int = 1_000,
    ) -> None:
        # 路径按调用方给出的字面值解析，不通过 HOME 展开 ``~``。
        self._root = Path(root).resolve()
        if not self._root.is_dir():
            raise ToolConfigurationError(f"filesystem root is not a directory: {root}")
        if min(max_read_bytes, max_write_bytes, max_list_entries) < 1:
            raise ToolConfigurationError("filesystem limits must be positive")
        self._allow_write = allow_write
        self._max_read_bytes = max_read_bytes
        self._max_write_bytes = max_write_bytes
        self._max_list_entries = max_list_entries
        self._spec = ToolSpec(
            name="filesystem",
            description="在受限工作区内读取、列出、检查或按策略写入文件。",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["read", "list", "exists", "stat", "write"],
                    },
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["operation", "path"],
                "additionalProperties": False,
            },
        )

    @property
    def spec(self) -> ToolSpec:
        """返回文件系统工具发现信息。"""
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """执行一次受限文件操作。

        Args:
            arguments: 包含 operation、path 和可选 content 的参数。
            context: 当前工具调用上下文。

        Returns:
            文件内容或结构化操作结果。

        Raises:
            ToolInputError: 参数非法、路径逃逸或操作越权。
        """
        del context
        operation = self._required_string(arguments, "operation")
        raw_path = self._required_string(arguments, "path")
        path = self._resolve(raw_path)
        if operation == "read":
            content = await asyncio.to_thread(self._read_text, path)
            return ToolResult(content)
        if operation == "list":
            content, truncated = await asyncio.to_thread(self._list_directory, path)
            return ToolResult(content, metadata={"truncated": truncated})
        if operation == "exists":
            return ToolResult(json.dumps({"exists": await asyncio.to_thread(self._exists, path)}))
        if operation == "stat":
            return ToolResult(await asyncio.to_thread(self._stat, path))
        if operation == "write":
            if not self._allow_write:
                raise ToolInputError("filesystem write operation is disabled")
            content = self._required_string(arguments, "content", allow_empty=True)
            written = await asyncio.to_thread(self._atomic_write, path, content)
            return ToolResult(
                json.dumps({"path": raw_path, "bytes_written": written}, ensure_ascii=False)
            )
        raise ToolInputError(f"unsupported filesystem operation: {operation}")

    def _resolve(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical.relative_to(self._root)
        except ValueError as exc:
            raise ToolInputError(f"path escapes filesystem root: {raw_path}") from exc
        self._guard_no_symlinks(lexical)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise ToolInputError(f"path escapes filesystem root: {raw_path}") from exc
        return resolved

    def _read_text(self, path: Path) -> str:
        self._guard_no_symlinks(path)
        if not path.is_file():
            raise ToolInputError(f"path is not a file: {path}")
        if path.stat().st_size > self._max_read_bytes:
            raise ToolInputError("file exceeds max_read_bytes")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolInputError("file is not valid UTF-8 text") from exc

    def _list_directory(self, path: Path) -> tuple[str, bool]:
        self._guard_no_symlinks(path)
        if not path.is_dir():
            raise ToolInputError(f"path is not a directory: {path}")
        entries: list[dict[str, object]] = []
        truncated = False
        for index, entry in enumerate(sorted(path.iterdir(), key=lambda item: item.name)):
            if index >= self._max_list_entries:
                truncated = True
                break
            entries.append(
                {
                    "name": entry.name,
                    "is_file": entry.is_file(),
                    "is_directory": entry.is_dir(),
                }
            )
        return json.dumps(entries, ensure_ascii=False), truncated

    def _stat(self, path: Path) -> str:
        self._guard_no_symlinks(path)
        if not path.exists():
            raise ToolInputError(f"path does not exist: {path}")
        stat = path.stat()
        return json.dumps(
            {
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "is_file": path.is_file(),
                "is_directory": path.is_dir(),
            }
        )

    def _atomic_write(self, path: Path, content: str) -> int:
        self._guard_no_symlinks(path)
        encoded = content.encode("utf-8")
        if len(encoded) > self._max_write_bytes:
            raise ToolInputError("content exceeds max_write_bytes")
        if not path.parent.is_dir():
            raise ToolInputError(f"parent directory does not exist: {path.parent}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            if path.exists():
                os.fchmod(descriptor, path.stat().st_mode)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # 再次检查父目录，缩小校验与替换之间的竞态窗口。
            self._guard_no_symlinks(path.parent)
            os.replace(temporary_path, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
        return len(encoded)

    def _exists(self, path: Path) -> bool:
        self._guard_no_symlinks(path)
        return path.exists()

    def _guard_no_symlinks(self, path: Path) -> None:
        """逐级拒绝符号链接，并在实际 I/O 前重复检查。

        该检查能够阻止普通链接逃逸并缩小 TOCTOU 窗口；它不等同于针对并发恶意修改
        文件系统的内核级沙箱，强对抗场景应替换为容器或专用文件服务。
        """
        try:
            relative = path.relative_to(self._root)
        except ValueError as exc:
            raise ToolInputError(f"path escapes filesystem root: {path}") from exc
        current = self._root
        if current.is_symlink():
            raise ToolInputError("filesystem root must not be a symbolic link")
        for part in relative.parts:
            if part in {".", ".."}:
                raise ToolInputError(f"path contains an unsafe component: {part}")
            current = current / part
            if current.is_symlink():
                raise ToolInputError(f"symbolic links are not allowed: {current}")

    @staticmethod
    def _required_string(
        arguments: Mapping[str, object],
        name: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ToolInputError(f"{name} must be a non-empty string")
        return value
