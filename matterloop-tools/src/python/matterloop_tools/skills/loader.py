"""受目录边界、符号链接和大小限制保护的 SKILL.md 加载器。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from matterloop_tools.skills.base import SkillContent, SkillSpec, validate_skill_name
from matterloop_tools.skills.errors import (
    SkillConfigurationError,
    SkillFormatError,
    SkillNotFoundError,
    SkillPathError,
    SkillTooLargeError,
)

_FRONTMATTER_KEYS = frozenset({"name", "description", "version"})
_REJECTED_SCALAR_PREFIXES = ("[", "{", "&", "*", "!", "|", ">")


@dataclass(frozen=True, slots=True)
class SkillLoaderConfig:
    """配置本地 Skill 根目录和资源上限。

    Args:
        root: 专用 Skill 根目录；其下一层目录名就是 Skill 名称。
        max_file_bytes: 单个 SKILL.md 的最大原始字节数。
        max_skills: 单次发现允许加载的最大 Skill 数量。
        max_frontmatter_lines: frontmatter 最大行数。
        max_scan_entries: 单次发现允许扫描的根目录项总数。
    """

    root: Path
    max_file_bytes: int = 256_000
    max_skills: int = 128
    max_frontmatter_lines: int = 32
    max_scan_entries: int = 1_024

    def __post_init__(self) -> None:
        """冻结绝对根路径并校验目录和资源上限。"""
        for name in (
            "max_file_bytes",
            "max_skills",
            "max_frontmatter_lines",
            "max_scan_entries",
        ):
            _validate_positive_integer(name, getattr(self, name))
        raw_root = Path(self.root)
        absolute_root = Path(os.path.abspath(raw_root))
        _guard_path_without_symlink(absolute_root)
        if not absolute_root.is_dir():
            raise SkillConfigurationError(f"skill root is not a directory: {raw_root}")
        object.__setattr__(self, "root", absolute_root)


class SkillLoader:
    """只从专用根目录下一层安全发现并加载 SKILL.md。

    Args:
        config: 已校验配置，或用于便捷构造配置的根目录。
        max_file_bytes: 便捷构造时单个文件最大字节数。
        max_skills: 便捷构造时单次发现最大 Skill 数。
        max_frontmatter_lines: 便捷构造时 frontmatter 最大行数。
        max_scan_entries: 便捷构造时单次发现最大根目录项总数。
    """

    def __init__(
        self,
        config: SkillLoaderConfig | str | Path,
        *,
        max_file_bytes: int | None = None,
        max_skills: int | None = None,
        max_frontmatter_lines: int | None = None,
        max_scan_entries: int | None = None,
    ) -> None:
        if isinstance(config, SkillLoaderConfig):
            if any(
                value is not None
                for value in (
                    max_file_bytes,
                    max_skills,
                    max_frontmatter_lines,
                    max_scan_entries,
                )
            ):
                raise SkillConfigurationError(
                    "loader limits must be configured either in SkillLoaderConfig or constructor"
                )
            self._config = config
            return
        self._config = SkillLoaderConfig(
            root=Path(config),
            max_file_bytes=max_file_bytes if max_file_bytes is not None else 256_000,
            max_skills=max_skills if max_skills is not None else 128,
            max_frontmatter_lines=(
                max_frontmatter_lines if max_frontmatter_lines is not None else 32
            ),
            max_scan_entries=max_scan_entries if max_scan_entries is not None else 1_024,
        )

    @property
    def config(self) -> SkillLoaderConfig:
        """返回不可变加载配置。"""
        return self._config

    def discover(self) -> tuple[SkillContent, ...]:
        """发现并加载所有直属 Skill 目录。

        所有文件会先完整加载；任意文件失败时不返回部分结果。符号链接条目会被明确
        拒绝，而普通文件和不含 SKILL.md 的目录会被忽略。

        Returns:
            按名称稳定排序的 Skill 内容。

        Raises:
            SkillPathError: 根目录下出现符号链接或非法文件类型。
            SkillTooLargeError: Skill 数量或文件大小超过上限。
            SkillFormatError: 任一文档格式非法。
        """
        entries: list[Path] = []
        for entry in self._config.root.iterdir():
            entries.append(entry)
            if len(entries) > self._config.max_scan_entries:
                raise SkillTooLargeError("scanned root entries exceed max_scan_entries")

        names: list[str] = []
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.is_symlink():
                raise SkillPathError(f"symbolic links are not allowed in skill root: {entry.name}")
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if skill_file.is_symlink():
                raise SkillPathError(f"symbolic links are not allowed: {entry.name}/SKILL.md")
            if not skill_file.exists():
                continue
            if not skill_file.is_file():
                raise SkillPathError(f"SKILL.md is not a regular file: {entry.name}")
            validate_skill_name(entry.name)
            names.append(entry.name)
            if len(names) > self._config.max_skills:
                raise SkillTooLargeError("discovered skills exceed max_skills")
        return tuple(self.load(name) for name in names)

    def load(self, name: str) -> SkillContent:
        """按经过校验的目录名加载一个 Skill。

        Args:
            name: Skill 目录名，不接受任意相对或绝对路径。

        Returns:
            解析后的不可变内容。

        Raises:
            SkillNameError: 名称包含路径或不符合命名规则。
            SkillNotFoundError: 目录或 SKILL.md 不存在。
            SkillPathError: 路径含符号链接或不是普通文件。
            SkillTooLargeError: 文件超过配置上限。
            SkillFormatError: UTF-8 或文档格式非法。
        """
        validate_skill_name(name)
        skill_directory = self._config.root / name
        skill_path = skill_directory / "SKILL.md"
        if not skill_directory.exists() or not skill_path.exists():
            raise SkillNotFoundError(name)
        self._guard_skill_path(skill_directory, skill_path)
        raw_content = self._read_bounded(name, skill_path)
        try:
            document = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillFormatError(f"skill is not valid UTF-8: {name}") from exc
        if "\x00" in document:
            raise SkillFormatError(f"skill contains NUL characters: {name}")
        metadata, markdown = self._parse_document(document, name)
        declared_name = metadata.get("name", name)
        if declared_name != name:
            raise SkillFormatError("frontmatter name must match the skill directory name")
        description = metadata.get("description") or self._infer_description(markdown, name)
        spec = SkillSpec(
            name=name,
            description=description,
            source=f"{name}/SKILL.md",
            version=metadata.get("version"),
        )
        return SkillContent(
            spec=spec,
            markdown=markdown,
            sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )

    def _guard_skill_path(self, directory: Path, skill_path: Path) -> None:
        try:
            directory.relative_to(self._config.root)
            skill_path.relative_to(self._config.root)
        except ValueError as exc:
            raise SkillPathError("skill path escapes configured root") from exc
        if directory.is_symlink() or skill_path.is_symlink():
            raise SkillPathError("symbolic links are not allowed for skills")
        if not directory.is_dir() or not skill_path.is_file():
            raise SkillPathError("skill source must be a regular SKILL.md file")
        _guard_path_without_symlink(directory)
        _guard_path_without_symlink(skill_path)
        resolved = skill_path.resolve(strict=True)
        try:
            resolved.relative_to(self._config.root)
        except ValueError as exc:
            raise SkillPathError("resolved skill path escapes configured root") from exc

    def _read_bounded(self, name: str, path: Path) -> bytes:
        expected_stat = self._lstat_regular_file(path)
        descriptor = self._open_skill_descriptor(name, path, expected_stat)
        try:
            content = self._read_descriptor_bounded(descriptor)
            self._verify_open_file_identity(path, descriptor, expected_stat)
            return content
        finally:
            os.close(descriptor)

    def _open_skill_descriptor(
        self,
        name: str,
        path: Path,
        expected_stat: os.stat_result,
    ) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
        supports_safe_openat = (
            os.open in os.supports_dir_fd
            and hasattr(os, "O_DIRECTORY")
            and getattr(os, "O_NOFOLLOW", 0) != 0
        )
        try:
            if not supports_safe_openat:
                descriptor = os.open(path, flags)
            else:
                root_descriptor = os.open(self._config.root, directory_flags)
                try:
                    skill_directory_descriptor = os.open(
                        name,
                        directory_flags,
                        dir_fd=root_descriptor,
                    )
                    try:
                        descriptor = os.open(
                            "SKILL.md",
                            flags,
                            dir_fd=skill_directory_descriptor,
                        )
                    finally:
                        os.close(skill_directory_descriptor)
                finally:
                    os.close(root_descriptor)
        except OSError as exc:
            raise SkillPathError("unable to open SKILL.md without following links") from exc
        try:
            self._verify_open_file_identity(path, descriptor, expected_stat)
        except SkillPathError:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _lstat_regular_file(path: Path) -> os.stat_result:
        """读取不跟随链接的文件快照并拒绝硬链接。"""
        try:
            file_stat = os.lstat(path)
        except OSError as exc:
            raise SkillPathError("unable to inspect SKILL.md") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise SkillPathError("SKILL.md must be a regular file")
        if file_stat.st_nlink != 1:
            raise SkillPathError("hard links are not allowed for SKILL.md")
        return file_stat

    @classmethod
    def _verify_open_file_identity(
        cls,
        path: Path,
        descriptor: int,
        expected_stat: os.stat_result,
    ) -> None:
        """确认路径快照、打开描述符和当前目录项始终指向同一文件。"""
        try:
            descriptor_stat = os.fstat(descriptor)
            current_stat = os.lstat(path)
        except OSError as exc:
            raise SkillPathError("SKILL.md changed while being opened") from exc
        for candidate in (descriptor_stat, current_stat):
            if not stat.S_ISREG(candidate.st_mode):
                raise SkillPathError("SKILL.md must be a regular file")
            if candidate.st_nlink != 1:
                raise SkillPathError("hard links are not allowed for SKILL.md")
            if not cls._same_file(expected_stat, candidate):
                raise SkillPathError("SKILL.md changed while being opened")

    @staticmethod
    def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
        """按设备号和 inode 判断两个文件快照是否属于同一对象。"""
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino

    def _read_descriptor_bounded(self, descriptor: int) -> bytes:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SkillPathError("SKILL.md must be a regular file")
        if file_stat.st_size > self._config.max_file_bytes:
            raise SkillTooLargeError("SKILL.md exceeds max_file_bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, self._config.max_file_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > self._config.max_file_bytes:
                raise SkillTooLargeError("SKILL.md exceeds max_file_bytes")
        return b"".join(chunks)

    def _parse_document(self, document: str, name: str) -> tuple[dict[str, str], str]:
        lines = document.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            markdown = document.strip()
            if not markdown:
                raise SkillFormatError(f"skill markdown must not be empty: {name}")
            return {}, markdown
        closing_index: int | None = None
        search_limit = min(len(lines), self._config.max_frontmatter_lines + 2)
        for index in range(1, search_limit):
            if lines[index].rstrip("\r\n") == "---":
                closing_index = index
                break
        if closing_index is None:
            raise SkillFormatError("frontmatter is not closed within max_frontmatter_lines")
        metadata: dict[str, str] = {}
        for line in lines[1:closing_index]:
            stripped = line.rstrip("\r\n")
            if not stripped:
                continue
            if ":" not in stripped:
                raise SkillFormatError("frontmatter only supports 'key: string' entries")
            key, raw_value = stripped.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if key not in _FRONTMATTER_KEYS:
                raise SkillFormatError(f"unsupported frontmatter key: {key}")
            if key in metadata:
                raise SkillFormatError(f"duplicate frontmatter key: {key}")
            metadata[key] = self._parse_scalar(value, key)
        markdown = "".join(lines[closing_index + 1 :]).strip()
        if not markdown:
            raise SkillFormatError(f"skill markdown must not be empty: {name}")
        return metadata, markdown

    @staticmethod
    def _parse_scalar(value: str, key: str) -> str:
        if not value or value.startswith(_REJECTED_SCALAR_PREFIXES):
            raise SkillFormatError(f"frontmatter {key} must be a single-line string")
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SkillFormatError(f"frontmatter {key} contains invalid JSON quoting") from exc
            if not isinstance(parsed, str):
                raise SkillFormatError(f"frontmatter {key} must be a string")
            value = parsed
        elif value.startswith("'") or "\t" in value:
            raise SkillFormatError(
                f"frontmatter {key} only supports plain text or JSON double quotes"
            )
        if "\r" in value or "\n" in value or not value.strip():
            raise SkillFormatError(f"frontmatter {key} must be a non-empty single-line string")
        return value

    @staticmethod
    def _infer_description(markdown: str, name: str) -> str:
        heading: str | None = None
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("```"):
                continue
            if line.startswith("#"):
                candidate = line.lstrip("#").strip()
                if candidate and heading is None:
                    heading = candidate
                continue
            if not line.startswith(("- ", "* ", "> ")):
                return line[:500]
        return (heading or f"本地 Skill：{name}")[:500]


def _guard_path_without_symlink(path: Path) -> None:
    """拒绝从文件系统锚点到目标的任意现存符号链接。"""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise SkillPathError(f"symbolic links are not allowed in skill path: {current}")


def _validate_positive_integer(name: str, value: object) -> None:
    """拒绝布尔值、整数子类和非正数资源上限。"""
    if type(value) is not int or value < 1:
        raise SkillConfigurationError(f"{name} must be a positive integer")
