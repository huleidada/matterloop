# matterloop-models

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-models` 提供供应商无关的异步模型协议、请求与响应 DTO、能力描述、测试客户端和支持安全热替换的模型注册表。OpenAI、DeepSeek、MiniMax、千问、智谱 GLM 及通用 OpenAI-compatible Chat Completions 适配器与中立 API 位于同一发行包，但必须从 `matterloop_models.providers` 按需导入。

本包不会读取 `.env`、进程环境、API Key、base URL、代理或证书配置。供应商 SDK 客户端必须由应用组合根创建并注入；模型名也没有隐式默认值。

## 安装与导入边界

```python
from matterloop_models import ModelRegistry, ModelRequest, ModelResponse
from matterloop_models.providers import DeepSeekChatModelClient, DeepSeekModelConfig
```

根命名空间只包含供应商中立 API。`matterloop_models.providers` 是稳定的供应商入口，不保留旧顶层兼容导入。

`matterloop-models[openai]` 只帮助宿主应用安装可用于 OpenAI-compatible 端点的异步 SDK；适配器源码不直接导入该 SDK。使用其他 SDK 或自定义客户端时可只安装基础发行包。

## 企业组合根

```python
from openai import AsyncOpenAI

from matterloop_models import ModelRegistry
from matterloop_models.providers import (
    DeepSeekChatModelClient,
    DeepSeekModelConfig,
    DeepSeekThinkingMode,
)


def build_models(sdk_client: AsyncOpenAI) -> ModelRegistry:
    client = DeepSeekChatModelClient(
        DeepSeekModelConfig(
            model="application-selected-model",
            thinking_mode=DeepSeekThinkingMode.ENABLED,
        ),
        client=sdk_client,
        owns_client=False,
    )
    registry = ModelRegistry()
    registry.register("planner", client, descriptor=client.descriptor)
    return registry
```

调用方负责：

- 从密钥服务或进程环境取得凭据并构造 SDK 客户端；
- 选择端点、地域、模型、代理、证书、连接池、SDK 超时和 SDK 重试；
- 决定客户端所有权，并在安全排空旧调用后关闭资源；
- 在日志、追踪、事件和持久化边界执行业务数据脱敏。

## 公共 API 分组

| 分组 | 公共类型 |
| --- | --- |
| 调用协议 | `ModelClient`、`ModelContinuation`、`ModelRequest`、`ModelResponse` |
| 消息与工具 | `MessageRole`、`ModelMessage`、`ToolChoice`、`ToolDefinition`、`ToolCall`、`ToolOutput` |
| 用量 | `TokenUsage` |
| 能力 | `CapabilityStatus`、`ModelCapabilities`、`ModelDescriptor`、`ModelFeature`、`ModelRequirements` |
| 注册表 | `ModelRegistry`、`ModelLease`、`ModelRetirement` |
| 自定义与测试 | `CallableModelClient`、`FakeModelClient` 及对应回调类型 |
| 异常 | `ModelError` 及各类型化子类 |

供应商子包公开 `OpenAIModelClient`、`DeepSeekChatModelClient`、`MiniMaxChatModelClient`、`QwenChatModelClient`、`ZhipuChatModelClient`、`GLMChatModelClient`、`OpenAICompatibleChatModelClient`，以及各自配置、continuation 和最小 SDK 协议。`GLM*` 是对应 `Zhipu*` 类型的别名。

## 中立请求与响应字段

### `ModelRequest`

| 字段 | 类型 | 默认值 | 约束与语义 |
| --- | --- | --- | --- |
| `messages` | `tuple[ModelMessage, ...]` | 必填 | 若没有消息，则必须存在 `tool_outputs` 或 `continuation` |
| `tools` | `tuple[ToolDefinition, ...]` | `()` | 本轮可见的函数工具 |
| `tool_outputs` | `tuple[ToolOutput, ...]` | `()` | 上一响应工具调用的本地执行结果 |
| `previous_response_id` | `str | None` | `None` | Responses 风格续轮标识；非空时不能是空白字符串 |
| `response_schema` | `Mapping | None` | `None` | 结构化输出 JSON Schema；适配器可能原生映射或提示降级 |
| `response_schema_name` | `str` | `"matterloop_response"` | 非空稳定名称 |
| `max_output_tokens` | `int | None` | `None` | 提供时至少为 1 |
| `temperature` | `float | None` | `None` | 通用层仅拒绝负数；供应商适配器可施加更严格边界 |
| `tool_choice` | `ToolChoice | None` | `None` | `AUTO`、`NONE`、`REQUIRED`；`None` 使用供应商默认行为 |
| `continuation` | `ModelContinuation | None` | `None` | 不透明私有续轮状态，`repr=False`，只能交还创建它的适配器 |
| `usage_scopes` | `tuple[str, ...]` | `()` | 去空白、非空且不得重复；供额度包装器多层汇总 |
| `metadata` | `Mapping[str, object]` | `{}` | 仅在 MatterLoop 内传播，不会自动脱敏 |

### 消息、工具和响应

| 类型 | 字段与默认值 |
| --- | --- |
| `ModelMessage` | `role`、`content` 必填；`name=None`；内容与显式名称不得为空 |
| `ToolDefinition` | `name`、`description`、`parameters` 必填；`strict=True` |
| `ToolCall` | `call_id`、`name` 必填；`arguments={}` |
| `ToolOutput` | `call_id`、`output` 必填；`is_error=False` |
| `TokenUsage` | `input_tokens=0`、`output_tokens=0`、`total_tokens=0`、`cache_hit_tokens=0`、`cache_miss_tokens=0`、`reasoning_tokens=0`；均不得为负数 |
| `ModelResponse` | `output_text=""`、`tool_calls=()`、`usage=TokenUsage()`、`response_id=None`、`continuation=None`、`metadata={}` |

`cache_hit_tokens` 与 `cache_miss_tokens` 是输入明细，`reasoning_tokens` 是输出明细；它们不应再次加到 `total_tokens`。供应商没有报告某项时保持零值，不能据此推断供应商一定没有产生该类 Token。

所有映射字段只冻结顶层。嵌套对象仍应由调用方视为不可变数据，不要在调用期间修改。

## 模型能力与选择

能力 DTO 的字段如下；这些字段只描述非敏感发现信息，不保存客户端、凭据或 continuation：

| DTO.字段 | 类型 | 必填 | 默认 | 业务含义 | 校验与持久化 |
| --- | --- | ---: | --- | --- | --- |
| `ModelCapabilities.supported` | `frozenset[ModelFeature]` | 否 | `frozenset()` | 明确支持的能力 | 不能与 `unsupported` 重叠；适合配置缓存 |
| `ModelCapabilities.unsupported` | `frozenset[ModelFeature]` | 否 | `frozenset()` | 明确不支持的能力 | 未出现在两侧的能力为 `UNKNOWN` |
| `ModelDescriptor.provider` | `str` | 是 | 无 | 稳定供应商标识 | 去除空白后必须非空；不应包含端点或租户凭据 |
| `ModelDescriptor.model` | `str` | 是 | 无 | 调用方配置的模型标识 | 去除空白后必须非空 |
| `ModelDescriptor.capabilities` | `ModelCapabilities` | 否 | 默认对象 | 当前适配器的三态能力快照 | 仅用于预检，不替代供应商运行时校验 |
| `ModelDescriptor.metadata` | `Mapping[str, object]` | 否 | `{}` | 非敏感发现元数据 | 顶层复制冻结；禁止放入 API key 和请求头 |
| `ModelRequirements.required_features` | `frozenset[ModelFeature]` | 否 | `frozenset()` | 调用方必须具备的能力 | 与 descriptor 做逐项三态匹配 |
| `ModelRequirements.provider` | `str \| None` | 否 | `None` | 可选供应商约束 | 非空值去除空白后必须非空 |
| `ModelRequirements.model` | `str \| None` | 否 | `None` | 可选模型约束 | 非空值去除空白后必须非空 |
| `ModelRequirements.allow_unknown` | `bool` | 否 | `False` | 是否允许未知能力通过预检 | 企业默认应保持 `False` |

`ModelFeature` 包含文本生成、developer 消息、工具调用、并行工具、具名工具选择、JSON Object、JSON Schema、response-id continuation、opaque continuation、reasoning 和 temperature。

`ModelCapabilities` 使用三态语义：

- `SUPPORTED`：适配器明确支持；
- `UNSUPPORTED`：适配器明确拒绝；
- `UNKNOWN`：代码没有作出能力承诺，不等同于不支持。

`ModelRequirements(required_features=frozenset(), provider=None, model=None, allow_unknown=False)` 默认拒绝未知能力。企业路由应使用 `ModelDescriptor` 与 `ModelRequirements.matches()` 做预检，但仍需处理供应商在具体模型或账号级别返回的能力错误。

## 注册表、租约和热替换

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `register` | `name, client, replace=False, descriptor=None` | 注册客户端；省略 descriptor 时尝试读取 `client.descriptor` |
| `get` | `name` | 返回查询时刻客户端快照；不提供长事务排空保证 |
| `describe` | `name` | 返回非敏感描述或 `None` |
| `acquire` | `name` | 调用时即固定客户端并增加活跃租约；返回同步/异步上下文管理器 |
| `swap` | `name, client, descriptor=None` | 原子换入新客户端，返回旧客户端的 `ModelRetirement` |
| `retire` | `name` | 移除名称并返回可等待排空的退役句柄 |
| `unregister` | `name` | 立即移除并返回客户端；需要安全关闭时应改用 `retire` |
| `names` | 无 | 返回排序后的名称快照 |

完整工具事务必须持有同一租约，否则热替换可能让 continuation 被发送到另一客户端：

```python
async with registry.acquire("worker") as model:
    first = await model.generate(request)
    # 工具输出续轮仍使用 model，而不是再次 get/acquire。

retirement = registry.swap("worker", replacement)
old_client = await retirement.wait_drained()
await old_client.aclose()  # 仅示意；按实际客户端接口关闭。
```

`register(..., replace=True)` 会退役旧槽位但不返回退役句柄；需要等待旧调用并关闭旧资源时必须使用 `swap()`。注册表从不启动或关闭客户端。`ModelLease.release()`、`aclose()` 与退役等待均可安全重复使用，但同一个租约上下文不能进入两次。

## Provider 配置

所有适配器构造函数都要求位置参数 `config` 和关键字参数 `client`；`owns_client=False`。只有显式设置 `owns_client=True` 时，适配器的 `aclose()` 才关闭注入客户端；支持该选项的客户端必须提供异步 `close()`。

| 配置 | 必填字段 | 可选字段及真实默认值 |
| --- | --- | --- |
| `OpenAIModelConfig` | `model` | 无 |
| `DeepSeekModelConfig` | `model` | `thinking_mode=ENABLED`、`reasoning_effort=None`、`enable_strict_tools=False` |
| `MiniMaxModelConfig` | `model` | 无 |
| `QwenModelConfig` | `model` | `thinking_mode=DEFAULT`、`thinking_budget=None`、`preserve_thinking=False`、`parallel_tool_calls=False` |
| `ZhipuModelConfig` / `GLMModelConfig` | `model` | `thinking_mode=DEFAULT`、`reasoning_effort=None`、`clear_thinking=True`、`do_sample=None` |
| `OpenAICompatibleChatConfig` | `provider`、`model` | `developer_role=SYSTEM`、`structured_output_mode=JSON_OBJECT`、`max_tokens_field=MAX_TOKENS`、`enable_strict_tools=False`、`preserve_reasoning_content=False`、`extra_parameters={}` |

通用兼容配置的 `extra_parameters` 会深复制、冻结且不参与 `repr`/比较，只能承载已核对的非敏感供应商参数。以下字段禁止放入其中：

- 适配器管理字段：`model`、`messages`、`tools`、`tool_choice`、`response_format`、`max_tokens`、`max_completion_tokens`、`temperature`、`stream`；
- 客户端字段：`api_key`、`authorization`、`base_url`、`default_headers`、`organization`、`project`。

## Provider 能力与限制

| Provider | API 与结构化输出 | continuation | Thinking / 工具限制 |
| --- | --- | --- | --- |
| OpenAI | Responses API；原生 JSON Object/JSON Schema | `previous_response_id`；拒绝 opaque continuation | 支持工具；能力描述未承诺通用 reasoning、temperature、并行或具名工具 |
| DeepSeek | Chat Completions；JSON Schema 映射为 `json_object` 并把 Schema 注入 system 提示 | 私有 `DeepSeekChatContinuation`；不使用 response id | developer 转 system；默认启用 thinking；thinking 开启时拒绝 temperature；strict tools 默认关闭 |
| MiniMax | OpenAI-compatible Chat；Schema 仅提示约束，未声明原生 JSON Mode | 私有 Chat continuation | 固定请求 `reasoning_split=true`；私有保留 `reasoning_details`；拒绝 `REQUIRED`；temperature 必须在 0–2 且为有限数 |
| Qwen | OpenAI-compatible Chat；非思考模式使用 JSON Object | 私有 Chat continuation | thinking=ENABLED 时拒绝 Schema 与 `REQUIRED`；可配置 thinking budget、保留思考和并行工具；默认不并行 |
| Zhipu / GLM | OpenAI-compatible Chat；JSON Object，不承诺原生 JSON Schema | 私有 Chat continuation | 私有回传 `reasoning_content`；拒绝 `REQUIRED`；temperature 最大 1；`NONE` 通过不发送工具表达；消息 name 会被移除 |
| Compatible | 由配置选择 JSON Object、JSON Schema 或 PROMPT_ONLY | 私有 `ChatCompletionsContinuation` | developer 角色、Token 字段、strict 和私有 reasoning 均由配置决定；response-id continuation 明确不支持 |

无论供应商提供 strict tools、JSON Mode 还是 JSON Schema，Agent 和工具注册表仍必须在本地严格验证模型输出与工具参数。

### DeepSeek

`DeepSeekThinkingMode` 为 `ENABLED | DISABLED`；`DeepSeekReasoningEffort` 为 `HIGH | MAX`。关闭 thinking 时不能设置 reasoning effort。developer 消息会转换为 system。工具响应必须携带适配器返回的 continuation，并且本地工具输出必须与全部待处理 `call_id` 精确匹配、不得重复。

assistant 的工具调用、工具输出及供应商要求回传的 `reasoning_content` 只保存在 continuation 私有历史中。continuation 绑定创建它的适配器实例和模型，不能跨注册表热替换、租户或端点复用。

### MiniMax

MiniMax 适配器不绑定国际站、中国站或具体模型。调用方负责按所用端点构造 SDK 客户端。`reasoning_details` 仅保存在不可打印 continuation 中；`ToolChoice.NONE` 通过不发送工具表达，`AUTO` 使用供应商默认行为。

### Qwen

`QwenThinkingMode` 为 `DEFAULT | ENABLED | DISABLED`。`thinking_budget` 提供时至少为 1；关闭 thinking 时不能配置 budget 或 `preserve_thinking=True`。当模式为 DEFAULT，但请求 Schema 或 `REQUIRED` 工具时，适配器会明确发送 `enable_thinking=false`，避免能力组合含糊。

### Zhipu / GLM

`ZhipuThinkingMode` 为 `DEFAULT | ENABLED | DISABLED`；reasoning effort 支持 `NONE`、`MINIMAL`、`LOW`、`MEDIUM`、`HIGH`、`XHIGH`、`MAX`。关闭 thinking 时不能设置 effort；`clear_thinking=False` 只允许与 `ENABLED` 同时使用。

### 通用 OpenAI-compatible

此适配器只适用于调用方已经核对协议差异的服务或私有网关。`ChatStructuredOutputMode` 为 `JSON_OBJECT | JSON_SCHEMA | PROMPT_ONLY`；`ChatMaxTokensField` 为 `MAX_TOKENS | MAX_COMPLETION_TOKENS`；`ChatDeveloperRole` 为 `SYSTEM | DEVELOPER`。配置描述的是适配器行为，不代表目标模型账号实际拥有该能力。

## 自定义客户端与离线测试

任意实现 `async generate(ModelRequest) -> ModelResponse` 的对象都满足 `ModelClient`。异步函数可用 `CallableModelClient` 包装：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `generate_callback` | 必填 | 必须返回 `ModelResponse` |
| `close_callback` | `None` | `aclose()` 最多调用一次 |
| `descriptor` | `None` | 可供注册表自动发现的非敏感描述 |

`FakeModelClient(responses=(), *, responder=None)` 支持预设队列或动态回调，两者互斥；请求会保存在 `requests` 中供测试断言。没有剩余响应时抛 `FakeModelExhaustedError`。测试请求可能包含业务敏感内容，不应把 Fake 客户端的请求快照用于生产日志。

## 错误分类与供应商映射

| 异常 | 含义 |
| --- | --- |
| `ModelAuthenticationError` | 认证或权限失败 |
| `ModelPaymentRequiredError` | 余额或付费状态拒绝调用 |
| `ModelRateLimitError` | 速率或并发限制 |
| `ModelServiceError` | 供应商 5xx 或已知服务故障码 |
| `ModelInvocationError` | 其他供应商调用失败 |
| `ModelResponseParseError` | 响应、工具参数或用量无法安全归一化；可能携带已发生计费的 `usage` |
| `ModelCapabilityError` | 当前 Provider、模型或模式不支持请求能力组合 |
| `ModelAlreadyRegisteredError` / `ModelNotFoundError` | 注册表名称冲突或缺失 |

HTTP 映射如下：OpenAI 与通用 compatible 把 401/403 映射为认证错误；DeepSeek 把 401 映射为认证错误；三者均把 402、429、5xx 分别映射为付费、限流和服务错误。其余错误只保留供应商名、HTTP 状态或原始异常类型，不传播 SDK 原始异常文本。

MiniMax 业务码映射：`1004/2049` 为认证，`1008` 为付费，`1002/2056` 为限流，`1000/1001/1024/1033/1041` 为服务错误。Zhipu 业务码映射：`1000–1004` 为认证，`1113` 为付费，`1302/1303/1304/1305/1308/1310/1313` 为限流，`500/1120/1230/1234/1312` 为服务错误；未命中业务码时再按通用 HTTP 映射处理。

`ModelResponseParseError.usage` 可能非空，因为远端调用已完成并产生费用后，本地解析仍可能失败。额度包装器应在重新抛出前按该 usage 结算。

## 敏感信息与持久化边界

- Provider continuation 的 `repr` 不展示历史、工具参数、`reasoning_content` 或 `reasoning_details`，公开响应也不暴露这些推理内容。
- continuation 只用于一次活跃模型事务，不应写入事件、检查点、缓存、数据库、追踪属性或日志。
- 安全异常不会拼接供应商原始响应、请求头或凭据，但应用日志仍可能记录调用方自己的消息、工具输出和业务异常。
- `ModelRequest.messages`、`tool_outputs`、`metadata` 与 `ModelResponse.output_text` 都可能包含敏感业务数据；本包不会自动脱敏。
- `ModelDescriptor.metadata` 和响应 metadata 只应保存非敏感、低基数诊断数据，不能放置密钥、Cookie 或 Authorization。
- 模型输出是不可信输入；在执行工具、渲染 HTML、生成查询或写入文件前必须经过本地校验与授权。

## 当前限制

- 仅提供异步模型协议，没有同步供应商客户端、CLI、凭据管理或模型发现服务。
- 不联网查询模型列表、价格、账号额度或区域可用性；模型名和价格均由调用方显式提供。
- Chat continuation 是进程内私有对象，不承诺跨进程或跨版本序列化。
- 能力描述以适配器实现为准，不替代供应商针对具体模型、端点和账号的官方能力检查。
- 适配器不执行自动重试。需要重试时应由 SDK 或上层策略配置，并同时设置调用次数、Token、费用和超时硬边界。
