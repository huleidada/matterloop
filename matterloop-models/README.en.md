[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-models/README.md) | English

# matterloop-models

`matterloop-models` gives upper-layer Agents a stable asynchronous model protocol while keeping provider differences
inside the `matterloop_models.providers` subpackage of the same distribution. Applications can use the built-in
adapters or implement `ModelClient` directly.

```bash
pip install matterloop-models
# Install the OpenAI-style SDK integration when needed
pip install "matterloop-models[openai]"
```

## The application creates the client

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

MatterLoop does not read `.env`, API keys, base URLs, proxy settings, or certificate configuration.
`owns_client=False` leaves responsibility for closing the SDK with the application. An adapter closes an injected
client only when `owns_client=True` is explicitly supplied.

## One model transaction

`ModelClient.generate()` accepts a provider-neutral `ModelRequest` and returns `ModelResponse`. Tool calling can span
multiple rounds, so the entire transaction should retain one registry lease:

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

This makes hot replacement affect only new transactions. A continuation is bound to the adapter, model, and internal
history that created it. It must not be reused across tenants, endpoints, or client instances, and it must not enter
logs, events, checkpoints, or databases.

## Provider-neutral request model

<details>
<summary>DTO field reference</summary>

- `ModelMessage(role, content, name)`: one system/developer/user/assistant/tool message.
- `ToolDefinition(name, description, parameters, strict)`: a function tool definition visible to the model.
- `ToolCall(call_id, name, arguments)` and `ToolOutput(call_id, output, is_error)`: a tool request and its local
  result.
- `ModelRequest(messages, tools, tool_outputs, previous_response_id, response_schema, response_schema_name, max_output_tokens, temperature, tool_choice, continuation, usage_scopes, metadata)`: one generation request.
- `TokenUsage(input_tokens, output_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens, reasoning_tokens)`:
  provider-reported usage. Cache and reasoning values are details and must not be added to the total again.
- `ModelResponse(output_text, tool_calls, usage, response_id, continuation, metadata)`: the normalized result.
- `ModelCapabilities(supported, unsupported)`: a three-state capability set. Capabilities present in neither set are
  unknown.
- `ModelDescriptor(provider, model, capabilities, metadata)`: a discoverable, non-sensitive model description in the
  registry.
- `ModelRequirements(required_features, provider, model, allow_unknown)`: routing preflight requirements; unknown
  capabilities are rejected by default.

`ModelRequest.metadata` and ordinary messages propagate through MatterLoop but are not redacted automatically.
Mapping fields are frozen only at the top level; do not mutate nested objects during a call.

</details>

`response_schema` is the basis for strict local validation. A provider adapter may use native JSON Schema, fall back
to JSON Object mode, or inject constraints into the prompt. Regardless of the mapping, the Agent must validate the
result again. A tool's `strict` flag also does not replace local argument validation and permission checks in
`ToolRegistry`.

## Registration, selection, and hot replacement

`ModelRegistry.register(name, client, replace, descriptor)` registers a name;
`ModelRegistry.acquire(name)` pins a client for a complete transaction; and
`ModelRegistry.swap(name, client, descriptor)` atomically installs a new instance and returns a retirement handle that
can wait for existing calls to drain.

```python
retirement = models.swap("planner", replacement, replacement.descriptor)
old_client = await retirement.wait_drained()
await old_client.aclose()
```

`get()` is suitable only for short lookups and provides no lifecycle guarantee across an `await`.
`register(..., replace=True)` does not return a drain handle for the old instance; use `swap()` when safe shutdown is
required. The registry itself neither starts nor closes clients.

Model capabilities use `SUPPORTED / UNSUPPORTED / UNKNOWN` to reject combinations known to be incompatible before a
call. They do not replace a provider's live capability checks. A model, account, or region can still reject a feature
at call time.

## Built-in providers

| Adapter | Protocol | Structured output | Continuation / thinking behavior |
| --- | --- | --- | --- |
| `OpenAIModelClient` | Responses API | JSON Object / JSON Schema | Uses response IDs; does not accept opaque continuation |
| `DeepSeekChatModelClient` | Chat Completions | Schema falls back to JSON Object plus prompt constraints | Converts developer to system; retains reasoning history privately |
| `MiniMaxChatModelClient` | OpenAI-compatible Chat | Prompt constraints | Retains `reasoning_details` privately |
| `QwenChatModelClient` | OpenAI-compatible Chat | JSON Object outside thinking mode | Thinking, budget, and parallel tools are controlled by configuration |
| `ZhipuChatModelClient` / `GLMChatModelClient` | OpenAI-compatible Chat | JSON Object | GLM is a Zhipu type alias; reasoning continuation stays private |
| `OpenAICompatibleChatModelClient` | Configurable Chat dialect | JSON Object, JSON Schema, or prompt fallback | Intended for gateways and services whose protocol differences have been verified |

Configuration objects do not provide a default model name:

- `OpenAIModelConfig(model)`.
- `DeepSeekModelConfig(model, thinking_mode, reasoning_effort, enable_strict_tools)`.
- `MiniMaxModelConfig(model)`.
- `QwenModelConfig(model, thinking_mode, thinking_budget, preserve_thinking, parallel_tool_calls)`.
- `ZhipuModelConfig(model, thinking_mode, reasoning_effort, clear_thinking, do_sample)`;
  `GLMModelConfig` is an alias of the same type.
- `OpenAICompatibleChatConfig(provider, model, developer_role, structured_output_mode, max_tokens_field, enable_strict_tools, preserve_reasoning_content, extra_parameters)`.

The generic compatible adapter is appropriate only for services already verified by the caller. `extra_parameters`
must not carry an API key, Authorization header, base URL, or request fields managed by the adapter. Capability
configuration describes local mapping behavior, not a provider guarantee.

Chat adapters continue a conversation with assistant tool calls, tool outputs, and any provider-required reasoning
continuation. Local output must cover every pending `call_id` exactly once and must not include unknown calls.

## Custom clients and testing

Any object that implements the following structural protocol can be integrated:

```python
class MyModelClient:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        ...
```

Use `CallableModelClient` to wrap a simple asynchronous function. `FakeModelClient` supports a queue of prepared
responses or a responder callback and retains requests for test assertions. Request snapshots may contain sensitive
business data and must not be used as production logs.

## Error boundary

Authentication, payment, rate limit, service failure, invocation failure, response parsing, and capability mismatch
errors are normalized into typed `ModelError` subclasses. Safe exceptions retain only the provider, status code, or
exception type and do not concatenate raw SDK text. If the remote call was billed but local parsing failed,
`ModelResponseParseError` may carry `usage`; a budget wrapper should settle that usage before re-raising the error.

Adapters do not retry automatically and do not query live model lists, prices, or account quotas. Configure retries in
the SDK or an upper-layer policy and combine them with hard call-count, Token, cost, and timeout limits. See the
[Enterprise integration guide](../docs/enterprise-integration.en.md) for multi-package composition.
