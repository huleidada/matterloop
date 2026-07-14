"""Shell 工具 argv、白名单与资源限制测试。"""

import json
import sys
from pathlib import Path

import pytest
from matterloop_tools import ShellTool, ToolConfigurationError, ToolContext, ToolInputError


async def test_shell_treats_metacharacters_as_plain_argument(tmp_path) -> None:
    marker = tmp_path / "created"
    executable = Path(sys.executable).name
    tool = ShellTool(
        tmp_path,
        allowed_commands={executable},
        base_environment={"PATH": str(Path(sys.executable).parent)},
    )
    result = await tool.invoke(
        {
            "argv": [
                executable,
                "-c",
                "import sys; print(sys.argv[1])",
                f"$(touch {marker})",
            ]
        },
        ToolContext("run"),
    )

    payload = json.loads(result.content)
    assert "$(touch" in payload["stdout"]
    assert not marker.exists()


async def test_shell_rejects_command_path_and_unlisted_environment(tmp_path) -> None:
    tool = ShellTool(tmp_path, allowed_commands={"python"})

    with pytest.raises(ToolInputError, match="not allowed"):
        await tool.invoke({"argv": ["/usr/bin/python", "-V"]}, ToolContext("run"))
    with pytest.raises(ToolInputError, match="environment variable"):
        await tool.invoke(
            {"argv": ["python", "-V"], "environment": {"PATH": "/tmp"}},
            ToolContext("run"),
        )


def test_shell_rejects_base_environment_with_custom_sandbox(tmp_path) -> None:
    """自定义沙箱与默认沙箱环境不得同时配置，避免产生被忽略的安全配置。"""

    class CustomSandbox:
        async def run(self, request):
            raise AssertionError(request)

    with pytest.raises(ToolConfigurationError, match="base_environment"):
        ShellTool(
            tmp_path,
            allowed_commands={"python"},
            sandbox=CustomSandbox(),
            base_environment={"PATH": "/explicit"},
        )
