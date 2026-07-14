"""通过可替换沙箱执行 argv 的 Shell 工具。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from matterloop_runtime import LocalProcessSandbox, ProcessRequest, Sandbox

from matterloop_tools.base import ToolContext, ToolResult, ToolSpec
from matterloop_tools.errors import ToolConfigurationError, ToolInputError


class ShellTool:
    """只允许白名单程序且从不解释 Shell 字符串。

    Args:
        workspace: 允许的工作目录根路径。
        allowed_commands: 可执行程序名白名单，例如 ``{"pytest", "ruff"}``。
        sandbox: 可选沙箱实现；默认使用本地进程边界。
        base_environment: 默认沙箱使用的显式基础环境；不会继承宿主环境。
        allowed_environment: 允许由调用参数设置的环境变量名。
        max_timeout_seconds: 单次执行的硬超时上限。
        max_output_bytes: 标准输出与错误输出共享上限。
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        allowed_commands: frozenset[str] | set[str],
        sandbox: Sandbox | None = None,
        base_environment: Mapping[str, str] | None = None,
        allowed_environment: frozenset[str] | set[str] = frozenset(),
        max_timeout_seconds: float = 60.0,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        if not allowed_commands or any(not command for command in allowed_commands):
            raise ToolConfigurationError("allowed_commands must not be empty")
        if max_timeout_seconds <= 0 or max_output_bytes < 1:
            raise ToolConfigurationError("shell limits must be positive")
        if sandbox is not None and base_environment is not None:
            raise ToolConfigurationError("base_environment cannot be used with a custom sandbox")
        # 路径按调用方给出的字面值解析，不通过 HOME 展开 ``~``。
        self._workspace = Path(workspace).resolve()
        if not self._workspace.is_dir():
            raise ToolConfigurationError(f"workspace is not a directory: {workspace}")
        self._allowed_commands = frozenset(allowed_commands)
        self._allowed_environment = frozenset(allowed_environment)
        self._max_timeout_seconds = max_timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._sandbox = sandbox or LocalProcessSandbox(
            self._workspace,
            base_environment=base_environment,
        )
        self._spec = ToolSpec(
            name="shell",
            description="在受限工作区内直接执行白名单 argv，不解释 Shell 表达式。",
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "cwd": {"type": "string"},
                    "environment": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "stdin": {"type": "string"},
                    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        )

    @property
    def spec(self) -> ToolSpec:
        """返回 Shell 工具发现信息。"""
        return self._spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """校验并直接执行一个 argv 请求。

        Raises:
            ToolInputError: 参数无效、命令未授权或超出资源上限。
        """
        del context
        argv = self._argv(arguments.get("argv"))
        self._check_command(argv[0])
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ToolInputError("cwd must be a string")
        timeout = arguments.get("timeout_seconds", self._max_timeout_seconds)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ToolInputError("timeout_seconds must be a positive number")
        if float(timeout) > self._max_timeout_seconds:
            raise ToolInputError("timeout_seconds exceeds configured maximum")
        environment = self._environment(arguments.get("environment", {}))
        stdin_value = arguments.get("stdin")
        if stdin_value is not None and not isinstance(stdin_value, str):
            raise ToolInputError("stdin must be a string")
        result = await self._sandbox.run(
            ProcessRequest(
                argv=argv,
                cwd=cwd,
                environment=environment,
                stdin=None if stdin_value is None else stdin_value.encode("utf-8"),
                timeout_seconds=float(timeout),
                max_output_bytes=self._max_output_bytes,
            )
        )
        content = json.dumps(
            {
                "return_code": result.return_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
            },
            ensure_ascii=False,
        )
        return ToolResult(
            content,
            is_error=result.return_code != 0 or result.timed_out,
            metadata={
                "return_code": result.return_code,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
            },
        )

    @staticmethod
    def _argv(value: object) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ToolInputError("argv must be an array of strings")
        argv: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item or "\x00" in item:
                raise ToolInputError("argv must contain non-empty strings without NUL bytes")
            argv.append(item)
        if not argv:
            raise ToolInputError("argv must contain at least one string")
        return tuple(argv)

    def _check_command(self, executable: str) -> None:
        # 只接受裸程序名，避免 `/tmp/pytest` 冒充白名单中的 `pytest`。
        if Path(executable).name != executable or executable not in self._allowed_commands:
            raise ToolInputError(f"command is not allowed: {executable}")

    def _environment(self, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ToolInputError("environment must be an object")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ToolInputError("environment keys and values must be strings")
            if key not in self._allowed_environment:
                raise ToolInputError(f"environment variable is not allowed: {key}")
            result[key] = item
        return result
