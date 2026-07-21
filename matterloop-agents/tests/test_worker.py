"""工具调用执行器的循环、授权与热替换测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from matterloop_agents import (
    ToolCallingWorker,
    ToolCallingWorkerConfig,
    UnauthorizedToolCallError,
)
from matterloop_core import LoopContext, LoopRequest, PlanStep
from matterloop_models import (
    FakeModelClient,
    ModelRegistry,
    ModelResponse,
    ToolCall,
)
from matterloop_models.providers import DeepSeekChatContinuation
from matterloop_tools import (
    ToolAccessScope,
    ToolContext,
    ToolEffect,
    ToolPermissionDeniedError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class StubTool:
    """只向执行器暴露工具描述。"""

    spec = ToolSpec(
        name="echo",
        description="回显输入",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


class EchoTool:
    """用于真实 ToolRegistry 联调的回显工具。"""

    spec = StubTool.spec

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        """返回带运行标识的确定性结果。"""
        return ToolResult(content=f"{context.run_id}:{arguments['text']}")


class WriteTool:
    """声明写副作用并记录是否越过注册表边界。"""

    spec = ToolSpec(
        name="write",
        description="写入测试数据",
        input_schema={"type": "object"},
        default_effect=ToolEffect.WRITE,
    )

    def __init__(self) -> None:
        self.invocations = 0

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self.invocations += 1
        return ToolResult("written")


class SwappingToolRegistry:
    """工具执行后替换模型，用于验证事务租约会固定原模型实例。"""

    def __init__(self, models: ModelRegistry, replacement: FakeModelClient) -> None:
        self._models = models
        self._replacement = replacement
        self.calls: list[tuple[str, Mapping[str, object], ToolContext]] = []

    def get(self, name: str) -> StubTool:
        """返回测试工具。"""
        assert name == "echo"
        return StubTool()

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        context: ToolContext,
    ) -> ToolResult:
        """记录调用，并在模型下一轮查询前完成热替换。"""
        self.calls.append((name, arguments, context))
        self._models.register("worker", self._replacement, replace=True)
        return ToolResult(content="echo result")


def test_worker_pins_model_during_tool_transaction_and_replaces_next_transaction() -> None:
    async def scenario() -> None:
        models = ModelRegistry()
        first_model = FakeModelClient(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(call_id="call_1", name="echo", arguments={"text": "hello"}),
                    ),
                    response_id="resp_1",
                ),
                ModelResponse(output_text="原事务步骤完成"),
            ]
        )
        replacement = FakeModelClient([ModelResponse(output_text="新事务使用替换模型")])
        models.register("worker", first_model)
        tools = SwappingToolRegistry(models, replacement)
        worker = ToolCallingWorker(
            models,
            tools,  # type: ignore[arg-type]
            ToolCallingWorkerConfig(model="worker", tool_names=("echo",)),
        )
        context = LoopContext(LoopRequest(goal="执行回显"), run_id="run-1")
        step = PlanStep(description="调用回显", step_id="step-1")

        first_result = await worker.execute(step, context)

        assert first_result.output == "原事务步骤完成"
        assert tools.calls[0][0] == "echo"
        assert tools.calls[0][2].run_id == "run-1"
        assert tools.calls[0][2].access_scope is ToolAccessScope.FULL
        assert first_model.requests[1].tool_outputs[0].output == "echo result"
        assert replacement.requests == ()
        assert first_result.metadata["total_tokens"] == 0

        second_result = await worker.execute(
            PlanStep(description="新事务", step_id="step-2"),
            LoopContext(LoopRequest(goal="使用热替换模型"), run_id="run-2"),
        )

        assert second_result.output == "新事务使用替换模型"
        assert replacement.requests[0].messages

    asyncio.run(scenario())


async def test_worker_enforces_read_only_scope_before_write_tool_invocation() -> None:
    model = FakeModelClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(call_id="call-write", name="write", arguments={}),),
                response_id="response-write",
            )
        ]
    )
    models = ModelRegistry()
    models.register("worker", model)
    write_tool = WriteTool()
    worker = ToolCallingWorker(
        models,
        ToolRegistry([write_tool]),
        ToolCallingWorkerConfig(model="worker", tool_names=("write",)),
    )
    context = LoopContext(
        LoopRequest(
            goal="尝试写入",
            metadata={"tool_access_scope": ToolAccessScope.READ_ONLY.value},
        ),
        run_id="child-run",
    )

    with pytest.raises(ToolPermissionDeniedError):
        await worker.execute(PlanStep(description="写入", step_id="write-step"), context)

    assert write_tool.invocations == 0


def test_worker_propagates_opaque_deepseek_continuation_without_response_id() -> None:
    async def scenario() -> None:
        continuation = DeepSeekChatContinuation(
            "deepseek-v4-flash",
            (
                {"role": "user", "content": "执行回显"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private reasoning",
                    "tool_calls": [
                        {
                            "id": "call-continuation",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"ok"}'},
                        }
                    ],
                },
            ),
        )
        model = FakeModelClient(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            call_id="call-continuation",
                            name="echo",
                            arguments={"text": "ok"},
                        ),
                    ),
                    continuation=continuation,
                ),
                ModelResponse(output_text="continuation 完成"),
            ]
        )
        models = ModelRegistry()
        models.register("worker", model)
        tools = ToolRegistry([EchoTool()])
        worker = ToolCallingWorker(
            models,
            tools,
            ToolCallingWorkerConfig(model="worker", tool_names=("echo",)),
        )

        result = await worker.execute(
            PlanStep(description="回显"),
            LoopContext(LoopRequest(goal="验证 DeepSeek 续轮"), run_id="run-continuation"),
        )

        assert result.output == "continuation 完成"
        assert model.requests[1].previous_response_id is None
        assert model.requests[1].continuation is continuation
        assert model.requests[1].tool_outputs[0].output == "run-continuation:ok"
        await tools.aclose()

    asyncio.run(scenario())


def test_worker_rejects_model_call_outside_tool_allowlist() -> None:
    async def scenario() -> None:
        models = ModelRegistry()
        models.register(
            "worker",
            FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=(ToolCall(call_id="call-unsafe", name="unsafe", arguments={}),),
                        response_id="resp-unsafe",
                    )
                ]
            ),
        )
        replacement = FakeModelClient()
        tools = SwappingToolRegistry(models, replacement)
        worker = ToolCallingWorker(
            models,
            tools,  # type: ignore[arg-type]
            ToolCallingWorkerConfig(model="worker", tool_names=("echo",)),
        )

        with pytest.raises(UnauthorizedToolCallError, match="unsafe"):
            await worker.execute(
                PlanStep(description="尝试工具"),
                LoopContext(LoopRequest(goal="安全执行")),
            )

        assert tools.calls == []

    asyncio.run(scenario())


def test_worker_integrates_with_real_tool_registry() -> None:
    async def scenario() -> None:
        models = ModelRegistry()
        models.register(
            "worker",
            FakeModelClient(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(call_id="call-echo", name="echo", arguments={"text": "ok"}),
                        ),
                        response_id="resp-echo",
                    ),
                    ModelResponse(output_text="真实注册表执行完成"),
                ]
            ),
        )
        tools = ToolRegistry([EchoTool()])
        worker = ToolCallingWorker(
            models,
            tools,
            ToolCallingWorkerConfig(model="worker", tool_names=("echo",)),
        )

        result = await worker.execute(
            PlanStep(description="回显"),
            LoopContext(LoopRequest(goal="验证注册表"), run_id="run-real"),
        )

        assert result.output == "真实注册表执行完成"
        model = models.get("worker")
        assert isinstance(model, FakeModelClient)
        assert model.requests[1].tool_outputs[0].output == "run-real:ok"
        await tools.aclose()

    asyncio.run(scenario())
