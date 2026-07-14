"""文件系统工具边界与原子写入测试。"""

import pytest
from matterloop_tools import FileSystemTool, ToolContext, ToolInputError


async def test_filesystem_is_read_only_by_default(tmp_path) -> None:
    tool = FileSystemTool(tmp_path)

    with pytest.raises(ToolInputError, match="disabled"):
        await tool.invoke(
            {"operation": "write", "path": "file.txt", "content": "value"},
            ToolContext("run"),
        )


async def test_filesystem_reads_and_atomically_writes_within_root(tmp_path) -> None:
    tool = FileSystemTool(tmp_path, allow_write=True)
    context = ToolContext("run")

    await tool.invoke(
        {"operation": "write", "path": "file.txt", "content": "你好"},
        context,
    )
    result = await tool.invoke({"operation": "read", "path": "file.txt"}, context)

    assert result.content == "你好"
    assert not tuple(tmp_path.glob(".file.txt.*"))


async def test_filesystem_rejects_parent_and_symlink_escape(tmp_path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    tool = FileSystemTool(root)

    with pytest.raises(ToolInputError, match="escapes"):
        await tool.invoke({"operation": "read", "path": "../secret"}, ToolContext("run"))
    with pytest.raises(ToolInputError, match="symbolic"):
        await tool.invoke({"operation": "list", "path": "link"}, ToolContext("run"))


async def test_filesystem_treats_tilde_as_literal_path(tmp_path, monkeypatch) -> None:
    """文件路径不得通过宿主 HOME 环境隐式展开。"""
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    literal_directory = tmp_path / "~"
    literal_directory.mkdir()
    (literal_directory / "source.txt").write_text("literal", encoding="utf-8")
    tool = FileSystemTool(tmp_path)

    result = await tool.invoke(
        {"operation": "read", "path": "~/source.txt"},
        ToolContext("run"),
    )

    assert result.content == "literal"
