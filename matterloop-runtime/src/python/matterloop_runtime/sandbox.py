"""本地受限进程执行协议与实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from matterloop_runtime.errors import SandboxPathError


def _copy_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """校验并复制由调用方显式提供的子进程环境。"""
    copied: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("environment keys and values must be strings")
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError("environment contains an invalid key or NUL byte")
        copied[key] = value
    return copied


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    """描述一次不经过系统 Shell 的进程请求。

    Args:
        argv: 可执行文件及参数，首项为程序名。
        cwd: 相对沙箱根目录或位于根目录内的绝对工作目录。
        environment: 追加到沙箱基础环境的变量。
        stdin: 写入进程标准输入的字节。
        timeout_seconds: 最长执行秒数。
        max_output_bytes: 标准输出和错误输出共享的最大保留字节数。
    """

    argv: tuple[str, ...]
    cwd: str | Path = "."
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    stdin: bytes | None = None
    timeout_seconds: float = 30.0
    max_output_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        """校验会影响进程边界的参数。"""
        if not self.argv or not self.argv[0]:
            raise ValueError("argv must contain an executable")
        if any("\x00" in value for value in self.argv):
            raise ValueError("argv must not contain NUL bytes")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be at least 1")
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(_copy_environment(self.environment)),
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """保存本地进程的受限输出和终止信息。"""

    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    truncated: bool = False


@runtime_checkable
class Sandbox(Protocol):
    """执行进程请求的可替换沙箱协议。"""

    async def run(self, request: ProcessRequest) -> ProcessResult:
        """执行进程并返回结构化结果。"""
        ...


@dataclass(slots=True)
class _OutputBudget:
    remaining: int
    truncated: bool = False

    def keep(self, chunk: bytes) -> bytes:
        if len(chunk) <= self.remaining:
            self.remaining -= len(chunk)
            return chunk
        kept = chunk[: self.remaining]
        self.remaining = 0
        self.truncated = True
        return kept


class LocalProcessSandbox:
    """使用 asyncio 子进程提供基础资源边界。

    警告：该实现不是恶意代码安全边界。它不会提供系统调用、网络、CPU、内存或用户
    权限隔离；需要执行不可信代码时必须替换为真正的容器或虚拟机沙箱。

    Args:
        root: 允许的工作目录根路径。
        base_environment: 调用方显式提供的子进程基础环境；默认完全为空，不继承宿主环境。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        # 路径按调用方给出的字面值解析，不通过 HOME 展开 ``~``。
        self._root = Path(root).resolve()
        self._base_environment = _copy_environment(base_environment or {})

    async def run(self, request: ProcessRequest) -> ProcessResult:
        """直接执行 argv，绝不使用 ``shell=True``。

        Args:
            request: 已校验的进程请求。

        Returns:
            带超时和截断标记的进程结果。

        Raises:
            SandboxPathError: 工作目录逃逸出沙箱根目录。
        """
        cwd = self._resolve_cwd(request.cwd)
        environment = {**self._base_environment, **request.environment}
        started_at = monotonic()
        process = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.PIPE
            if request.stdin is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        budget = _OutputBudget(request.max_output_bytes)
        stdout_task = asyncio.create_task(self._read_stream(process.stdout, budget))
        stderr_task = asyncio.create_task(self._read_stream(process.stderr, budget))
        if request.stdin is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(request.stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return ProcessResult(
            return_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_seconds=monotonic() - started_at,
            timed_out=timed_out,
            truncated=budget.truncated,
        )

    def _resolve_cwd(self, cwd: str | Path) -> Path:
        candidate = Path(cwd)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise SandboxPathError(f"working directory escapes sandbox root: {cwd}") from exc
        if not resolved.is_dir():
            raise SandboxPathError(f"working directory does not exist: {cwd}")
        return resolved

    @staticmethod
    async def _read_stream(
        stream: asyncio.StreamReader,
        budget: _OutputBudget,
    ) -> bytes:
        output = bytearray()
        while chunk := await stream.read(64 * 1024):
            output.extend(budget.keep(chunk))
        return bytes(output)
