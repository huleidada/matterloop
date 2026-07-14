"""本地进程沙箱基础边界测试。"""

import sys

import pytest
from matterloop_runtime import LocalProcessSandbox, ProcessRequest, SandboxPathError


async def test_sandbox_does_not_interpret_shell_syntax(tmp_path) -> None:
    sandbox = LocalProcessSandbox(tmp_path)
    marker = tmp_path / "created"
    result = await sandbox.run(
        ProcessRequest(
            argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", f"$(touch {marker})"),
            cwd=".",
        )
    )

    assert result.return_code == 0
    assert "$(touch" in result.stdout
    assert not marker.exists()


async def test_sandbox_enforces_timeout_and_output_limit(tmp_path) -> None:
    sandbox = LocalProcessSandbox(tmp_path)
    timeout = await sandbox.run(
        ProcessRequest(
            argv=(sys.executable, "-c", "import time; time.sleep(2)"),
            timeout_seconds=0.05,
        )
    )
    limited = await sandbox.run(
        ProcessRequest(
            argv=(sys.executable, "-c", "print('x' * 10000)"),
            max_output_bytes=32,
        )
    )

    assert timeout.timed_out
    assert limited.truncated
    assert len(limited.stdout.encode()) <= 32


async def test_sandbox_rejects_working_directory_escape(tmp_path) -> None:
    sandbox = LocalProcessSandbox(tmp_path)

    with pytest.raises(SandboxPathError):
        await sandbox.run(ProcessRequest(argv=(sys.executable, "-V"), cwd=".."))


async def test_sandbox_uses_only_explicit_environment(tmp_path, monkeypatch) -> None:
    """默认子进程环境应为空，显式基础环境和请求环境才会进入进程。"""
    monkeypatch.setenv("PATH", "/host/path/must/not/leak")
    monkeypatch.setenv("MATTERLOOP_HOST_SECRET", "must-not-leak")
    script = (
        "import os; "
        "print(os.environ.get('PATH', '<missing>')); "
        "print(os.environ.get('MATTERLOOP_HOST_SECRET', '<missing>')); "
        "print(os.environ.get('EXPLICIT_VALUE', '<missing>'))"
    )

    isolated = await LocalProcessSandbox(tmp_path).run(
        ProcessRequest(argv=(sys.executable, "-c", script))
    )
    explicit = await LocalProcessSandbox(
        tmp_path,
        base_environment={"PATH": "/explicit/path"},
    ).run(
        ProcessRequest(
            argv=(sys.executable, "-c", script),
            environment={"EXPLICIT_VALUE": "provided"},
        )
    )

    assert isolated.stdout.splitlines() == ["<missing>", "<missing>", "<missing>"]
    assert explicit.stdout.splitlines() == ["/explicit/path", "<missing>", "provided"]


def test_process_request_repr_hides_environment_values() -> None:
    """显式环境值不得因数据类 repr 进入日志。"""
    request = ProcessRequest(
        argv=(sys.executable, "-V"),
        environment={"ACCESS_TOKEN": "sensitive-value"},
    )

    assert "ACCESS_TOKEN" not in repr(request)
    assert "sensitive-value" not in repr(request)
