# matterloop-models

`matterloop-models` 给上层 Agent 一个稳定的异步模型协议，同时把供应商差异留在同一发行包的
`matterloop_models.providers` 子包。应用可以使用内置适配器，也可以直接实现 `ModelClient`。

```bash
pip install matterloop-models
# 需要 OpenAI 风格 SDK 时
pip install "matterloop-models[openai]"
```

## 客户端由应用创建

```python
from openai import AsyncOpenAI

from matterloop_models import ModelRegistry, ModelRequest
from matterloop_models.providers import DeepSeekChatModelClient, DeepSeekModelConfig

sdk = AsyncOpenAI(api_key=secret, base_url=endpoint)
model = DeepSeekChatModelClient(
    DeepSeekModelConfig(model="application-selected-model"),
    client=sdk,
    owns_client=False,
)

models = ModelRegistry()
models.register("planner", model, descriptor=model.descriptor)
```

MatterLoop 不读取 `.env`、API key、base URL、代理或证书配置。`owns_client=False` 表示 SDK 的关闭
责任仍在应用；只有明确传 `owns_client=True` 时，适配器才会在关闭时处理注入客户端。

## 一次模型事务

`ModelClient.generate()` 接收中立 `ModelRequest`，返回 `ModelResponse`。工具调用可能需要多轮，
整个事务应持有同一注册表租约：

```python
async with models.acquire("worker") as client:
    response = await client.generate(request)
    while response.tool_calls:
        outputs = await execute_tools(response.tool_calls)
        response = await client.generate(
            ModelRequest(
                messages=(),
                tools=request.tools,
                tool_outputs=tuple(outputs),
                previous_response_id=response.response_id,
                continuation=response.continuation,
            )
        )
```

这样热替换只影响新事务。continuation 绑定创建它的适配器、模型和内部历史，不能跨租户、端点或
客户端实例复用，也不应进入日志、事件、checkpoint 或数据库。

## 中立请求模型

<details>
<summary>DTO 字段速查</summary>

- `ModelMessage(role, content, name)`：一条 system/developer/user/assistant/tool 消息。
- `ToolDefinition(name, description, parameters, strict)`：模型可见的函数工具定义。
- `ToolCall(call_id, name, arguments)` 与 `ToolOutput(call_id, output, is_error)`：调用及本地结果。
- `ModelRequest(messages, tools, tool_outputs, previous_response_id, response_schema, response_schema_name, max_output_tokens, temperature, tool_choice, continuation, usage_scopes, metadata)`：一次生成请求。
- `TokenUsage(input_tokens, output_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens, reasoning_tokens)`：供应商报告的用量；缓存和 reasoning 是明细，不应重复加到总量。
- `ModelResponse(output_text, tool_calls, usage, response_id, continuation, metadata)`：归一化结果。
- `ModelCapabilities(supported, unsupported)`：能力三态集合；未出现在两者中的能力为 unknown。
- `ModelDescriptor(provider, model, capabilities, metadata)`：注册表中可发现的非敏感模型描述。
- `ModelRequirements(required_features, provider, model, allow_unknown)`：路由预检条件，默认不接受 unknown。

`ModelRequest.metadata` 和普通消息会在 MatterLoop 内传播，但不会自动脱敏。映射字段只冻结顶层，
调用期间不要修改嵌套对象。

</details>

`response_schema` 是本地严格校验的依据。供应商适配器可能使用原生 JSON Schema、降级为 JSON
Object，或把约束写入提示；无论哪种方式，Agent 都必须再次验证结果。工具的 `strict` 标志也不能
替代 `ToolRegistry` 的本地参数校验与权限检查。

## 注册、选择与热替换

`ModelRegistry.register(name, client, replace, descriptor)` 注册名称；
`ModelRegistry.acquire(name)` 为完整事务固定客户端；
`ModelRegistry.swap(name, client, descriptor)` 原子换入新实例并返回可等待排空的 retirement。

```python
retirement = models.swap("planner", replacement, replacement.descriptor)
old_client = await retirement.wait_drained()
await old_client.aclose()
```

`get()` 只适合短查询，不提供跨 await 的生命周期保证。`register(..., replace=True)` 不返回旧实例
排空句柄；需要安全关闭时使用 `swap()`。注册表本身不启动或关闭客户端。

模型能力使用 `SUPPORTED / UNSUPPORTED / UNKNOWN`，目的是在调用前拒绝已知不兼容组合，而不是
替代供应商的实时能力检查。模型、账号或地域仍可能在调用时拒绝某项能力。

## 内置 Provider

| 适配器 | 协议 | 结构化输出 | continuation / thinking 要点 |
| --- | --- | --- | --- |
| `OpenAIModelClient` | Responses API | JSON Object / JSON Schema | 使用 response id；不接受 opaque continuation |
| `DeepSeekChatModelClient` | Chat Completions | Schema 降级为 JSON Object + 提示约束 | developer 转 system；私有保存 reasoning history |
| `MiniMaxChatModelClient` | OpenAI-compatible Chat | 提示约束 | 私有保存 `reasoning_details` |
| `QwenChatModelClient` | OpenAI-compatible Chat | 非思考模式 JSON Object | thinking、budget 与并行工具由配置控制 |
| `ZhipuChatModelClient` / `GLMChatModelClient` | OpenAI-compatible Chat | JSON Object | GLM 是智谱类型别名；私有保存 reasoning continuation |
| `OpenAICompatibleChatModelClient` | 可配置 Chat 方言 | JSON Object、JSON Schema 或提示降级 | 适合已核对协议差异的网关和其他服务 |

配置对象没有模型名默认值：

- `OpenAIModelConfig(model)`。
- `DeepSeekModelConfig(model, thinking_mode, reasoning_effort, enable_strict_tools)`。
- `MiniMaxModelConfig(model)`。
- `QwenModelConfig(model, thinking_mode, thinking_budget, preserve_thinking, parallel_tool_calls)`。
- `ZhipuModelConfig(model, thinking_mode, reasoning_effort, clear_thinking, do_sample)`；
  `GLMModelConfig` 是同类型别名。
- `OpenAICompatibleChatConfig(provider, model, developer_role, structured_output_mode, max_tokens_field, enable_strict_tools, preserve_reasoning_content, extra_parameters)`。

通用 compatible 适配器只适合调用方已经验证过的服务。`extra_parameters` 禁止承载 API key、
Authorization、base URL 和适配器自行管理的请求字段；能力配置描述的是本地映射方式，不是供应商
承诺。

各 Chat 适配器会把 assistant tool calls、tool outputs 和供应商要求的 reasoning continuation 一起
续轮。本地输出必须覆盖全部待处理 `call_id`，不能重复或夹带未知调用。

## 自定义与测试

任意对象只要实现下面的结构协议即可接入：

```python
class MyModelClient:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        ...
```

简单异步函数可用 `CallableModelClient` 包装。`FakeModelClient` 支持预设响应队列或 responder 回调，
并保留请求供测试断言；请求快照可能包含敏感业务数据，不应拿去做生产日志。

## 错误边界

认证、付费、限流、服务故障、调用失败、响应解析和能力不匹配分别归一化为类型化 `ModelError`。
安全异常只保留供应商、状态码或异常类型，不拼接 SDK 原始文本。远端已计费但本地解析失败时，
`ModelResponseParseError` 可能携带 `usage`，预算包装器应先结算再抛出。

适配器不自动重试，也不查询实时模型列表、价格或账户额度。重试应在 SDK 或上层策略中配置，并与
调用次数、Token、费用和超时硬边界同时使用。多模块装配见
[企业集成指南](../docs/enterprise-integration.md)。
