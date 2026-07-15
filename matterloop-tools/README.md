# matterloop-tools

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-tools` 提供供应商无关的异步工具协议、统一注册与授权入口、MCP 客户端适配、
本地 Skill 发现，以及受边界约束的文件、进程和 HTTP 工具。

工具不会读取 `.env` 或自动继承宿主环境。内置工具以减少误用和路径/协议逃逸为目标，但不是恶意代码、恶意同机进程或不可信网络的安全隔离边界。

## 企业装配原则

```text
Agent / Worker
      │ name + arguments + ToolContext
      ▼
ToolRegistry
      ├─ RuntimeContainer.acquire(name)
      ├─ ToolAuthorizer.authorize(...)
      └─ Tool.invoke(...)
             ├─ FileSystemTool / ShellTool / HttpTool
             ├─ McpToolAdapter -> McpServerRegistry -> injected Session
             └─ SkillTool -> SkillContextAdapter -> SkillRegistry

Host / Control Plane
      └─ McpServerRegistry
             ├─ tools: list / call（可适配为 Tool）
             ├─ resources: list / read
             ├─ resource templates: list
             └─ prompts: list / get
```

`ToolRegistry` 的真实默认授权器是 `AllowAllToolAuthorizer`，即显式全放行。生产环境不得把“经过注册表”误解为“已经安全授权”；必须注入 `RuleBasedPermissionPolicy` 或实现自己的 `ToolAuthorizer`。

```python
from matterloop_policies import (
    PermissionRule,
    RuleBasedPermissionPolicy,
)
from matterloop_tools import (
    FileSystemTool,
    PermissionDecision,
    ToolRegistry,
)

authorizer = RuleBasedPermissionPolicy(
    rules=(
        PermissionRule(
            tool="filesystem",
            operations=("read", "list", "exists", "stat"),
            decision=PermissionDecision.ALLOW,
        ),
    ),
)
tools = ToolRegistry(
    [FileSystemTool("./workspace")],
    authorizer=authorizer,
)
```

## 公共 API

| 分组 | 公共类型 |
| --- | --- |
| DTO | `ToolSpec`、`ToolContext`、`ToolResult` |
| 协议 | `Tool`、`ToolAuthorizer` |
| 授权 | `PermissionDecision`、`AllowAllToolAuthorizer` |
| 注册表 | `ToolRegistry` |
| 内置工具 | `FileSystemTool`、`ShellTool`、`HttpTool` |
| MCP | `McpServerConnection`、`McpServerRegistry`、`McpToolAdapter`、`McpSdkV1SessionAdapter` |
| Skills | `SkillLoader`、`SkillRegistry`、`SkillContextAdapter`、`SkillTool` |
| 异常 | `ToolError`、`ToolNotFoundError`、`ToolPermissionDeniedError`、`ToolInputError`、`ToolConfigurationError` |

## 公共 DTO 与协议

### DTO 字段

| 类型 | 字段 | 默认值与约束 |
| --- | --- | --- |
| `ToolSpec` | `name`、`description`、`input_schema` | 全部必填；名称和描述不得为空；Schema 顶层只读 |
| `ToolContext` | `run_id`、`step_id`、`metadata` | `run_id` 必填非空；`step_id=None`；`metadata={}`，仅允许 JSON 兼容值并递归复制、冻结 |
| `ToolResult` | `content`、`is_error`、`metadata` | `content` 必填；`is_error=False`；`metadata={}` 且顶层只读 |
| `PermissionDecision` | `ALLOW`、`DENY` | 授权器必须返回枚举值；任何非 `ALLOW` 结果都被注册表拒绝 |

`ToolSpec.input_schema` 与 `ToolResult.metadata` 只冻结顶层，嵌套值仍应被视为不可变；
`ToolContext.metadata` 会递归快照并冻结。上述字段均不会自动脱敏。

### 结构协议

```python
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult: ...


class ToolAuthorizer(Protocol):
    async def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> PermissionDecision: ...
```

`ToolSpec.input_schema` 用于模型发现，不代表注册表会自动执行 JSON Schema 校验。内置工具自行做本地类型和边界校验；自定义工具必须同样验证所有参数。模型侧 strict tools 不能替代本地授权与校验。

## `ToolRegistry`

### 构造参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `tools` | `()` | 无需异步启动的初始工具；名称不得重复 |
| `authorizer` | `None` | `None` 会使用全放行的 `AllowAllToolAuthorizer` |

### 方法与生命周期

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `register` | `tool, replace=False` | 先执行可选 `start()`，成功后注册或替换 |
| `replace` | `name, tool` | 工具 `spec.name` 必须等于 name；安全热替换 |
| `unregister` | `name` | 从新调用中移除；旧调用结束后执行可选 `aclose()` |
| `get` | `name` | 返回当前工具，适合读取 `spec`；长调用应走 `invoke` |
| `names` | 无 | 返回稳定排序名称 |
| `specs` | 无 | 返回按名称排序的 `ToolSpec` 发现快照 |
| `invoke` | `name, arguments, context=` | 先借用调用期固定实例，再针对同一参数快照授权并执行 |
| `aclose` | 无 | 阻止新调用；立即关闭空闲工具，活跃工具在调用退出后关闭 |

热替换由 `RuntimeContainer` 管理：新实例只有在 `start()` 成功后才原子换入；启动失败会尽力关闭新实例并保留旧实例。已经开始的调用继续使用旧工具，最后一个旧调用结束后才关闭旧实例。
替换提交后的旧实例若在 `aclose()` 中抛错，该关闭错误仍可能向当前调用方传播，但新实例不会
回滚；企业组件的关闭应保持幂等并自行上报清理失败，调用方不能把所有关闭异常解释为替换未提交。

工具租约覆盖授权和执行，防止同名工具在授权完成后被热替换成另一实现。注册表会递归复制并冻结
JSON 兼容参数，再分别向授权器和工具提供等值副本；调用方并发修改原始嵌套对象不会改变本次
决策。授权器应根据调用方身份、租户、工具名、具体参数和运行上下文作出决定。注册表关闭后，
Runtime 的 `RuntimeClosedError` 可能直接向上传播；它不是 `ToolError` 子类。

## MCP 集成

MCP 集成分为稳定协议层和可选 SDK 桥接层。自定义 `McpSessionAdapter` 不需要安装额外依赖；
使用官方 Python SDK v1 桥接器时安装：

```bash
uv add "matterloop-tools[mcp]"
```

`McpServerConnection` 不创建 stdio 子进程、HTTP 客户端、OAuth 客户端或 Session。宿主必须先
构造并进入连接上下文，再将 Session 交给 `McpSdkV1SessionAdapter`。因此端点、请求头、代理、
证书、凭据和进程环境始终留在组合根。

### MCP 公共字段

| 类型 | 字段 | 默认值与约束 | 安全与生命周期 |
| --- | --- | --- | --- |
| `McpLimits` | `request_timeout_seconds` | `30.0`，有限正数 | 每个业务请求独立生效 |
| `McpLimits` | `initialize_timeout_seconds` | `15.0`，有限正数 | 限制能力协商时间 |
| `McpLimits` | `close_timeout_seconds` | `10.0`，有限正数 | 仅关闭受托管 Session |
| `McpLimits` | `max_pages` | `20`，正整数 | 单类完整发现的页数上限 |
| `McpLimits` | `max_items` | `1_000`，正整数 | 单类完整发现的条目上限 |
| `McpLimits` | `max_content_blocks` | `256`，正整数 | 单次工具、资源或 Prompt 结果的内容块上限 |
| `McpLimits` | `max_result_characters` | `200_000`，正整数 | MCP Tool 转为 `ToolResult` 后的文本上限 |
| `McpServerConfig` | `name` | 必填非空 | 注册表标识，不发送给远端 |
| `McpServerConfig` | `tool_namespace` | 必填非空 | 生成模型可见工具名，避免多服务冲突 |
| `McpServerConfig` | `limits` | `McpLimits()` | 不进入远端请求 |
| `McpServerConfig` | `initialize_on_start` | `True` | 注册连接时执行一次 `initialize()` |
| `McpServerConfig` | `owns_session` | `False` | 为 `True` 时连接关闭会调用 Adapter 的 `aclose()` |
| `McpServerCapabilities` | `tools/resources/prompts/completions/logging` | 均为 `None` | `True` 表示已声明，`False` 表示初始化结果明确未声明，`None` 表示未协商并采用兼容模式 |
| `McpContent` | `kind` | 必填枚举 | `TEXT/JSON/IMAGE/AUDIO/RESOURCE/BINARY/UNKNOWN` |
| `McpContent` | `text/data/mime_type/uri` | 默认 `None` | 可能包含敏感远端内容，不应直接写日志 |
| `McpContent` | `metadata` | `{}`，顶层冻结 | 不会自动脱敏或持久化 |
| `McpToolDefinition` | `name` | 必填非空 | 原始远端名，仅适配后名称暴露给模型 |
| `McpToolDefinition` | `description` | `""` | 远端不可信文本，会进入模型上下文 |
| `McpToolDefinition` | `input_schema` | `{"type": "object"}` | 顶层冻结；远端和工具仍需执行参数校验 |
| `McpToolDefinition` | `output_schema` | `None` | 仅作为声明，不替代结果验证 |
| `McpToolDefinition` | `annotations` | `{}` | 远端提示信息，不能替代本地权限策略 |
| `McpResourceDefinition` | `uri/name` | 必填非空 | URI 由宿主决定是否允许读取 |
| `McpResourceDefinition` | `description/mime_type/size/metadata` | 空值或 `None` | `size` 若存在不得为负数 |
| `McpResourceTemplateDefinition` | `uri_template/name` | 必填非空 | 参数填充与业务授权由宿主负责 |
| `McpResourceTemplateDefinition` | `description/mime_type/metadata` | 空值、`None` 或 `{}` | 远端声明，不自动信任或持久化 |
| `McpPromptArgument` | `name/description/required` | 名称必填；说明为空；默认非必填 | SDK v1 获取 Prompt 时参数键和值必须是字符串 |
| `McpPromptDefinition` | `name/description/arguments` | 名称必填，其余为空 | Prompt 是远端不可信内容，不自动提升为系统指令 |
| `McpCallResult` | `content/structured_content/is_error/metadata` | 内容为空、映射为空、`False`、映射为空 | MCP Tool Adapter 只输出标准内容与安全统计，不转发远端 metadata |
| `McpResourceResult` | `contents/metadata` | 内容必填，metadata 为空 | 资源内容由调用方决定是否进入模型上下文 |
| `McpPromptMessage` | `role/content` | 角色非空，内容元组必填 | 角色仍需由宿主映射到允许的模型消息角色 |
| `McpPromptResult` | `messages/description/metadata` | 消息必填，其余为空 | 不自动写入 Agent 上下文或审计存储 |
| `McpToolPage/McpResourcePage/McpResourceTemplatePage/McpPromptPage` | `items/next_cursor` | items 必填；cursor 默认 `None` | cursor 是远端不透明值，只由连接分页器使用 |
| `McpCatalog` | `tools/resources/resource_templates/prompts` | 全部必填元组 | 是单个连接租约内顺序读取的目录快照 |

`McpCallResult`、`McpResourceResult` 和 `McpPromptResult` 保存标准化结果；其内容不会进入
MatterLoop checkpoint，除非调用方主动放入 Loop 输出或 metadata。`StructuralMcpResponseMapper`
兼容 Mapping、dataclass、Pydantic 和普通属性对象；自定义 SDK 改变字段语义时应注入自己的
`McpResponseMapper`。

### MCP 操作与并发语义

| 构造器 | 参数 | 默认值与所有权 |
| --- | --- | --- |
| `McpServerConnection` | `session` | 必填，满足 `McpSessionAdapter`；端点与凭据已由宿主配置 |
| `McpServerConnection` | `config` | 必填 `McpServerConfig` |
| `McpServerConnection` | `mapper` | `None`，使用 `StructuralMcpResponseMapper` |
| `McpSdkV1SessionAdapter` | `session` | 必填且必须是已进入上下文的官方 v1 `ClientSession` |
| `McpSdkV1SessionAdapter` | `close_callback` | `None`，默认不退出宿主 Session；显式回调幂等执行一次 |
| `McpToolAdapter` | `caller/server_name/namespace/definition` | 全部必填；通常由 `discover_tools()` 构造 |
| `McpToolAdapter` | `max_result_characters` | 必填正整数；结果按预算增量渲染，不先复制完整正文或 JSON |
| `McpToolAdapter` | `max_content_blocks` | `256`，正整数；空内容块同样计数 |
| `McpToolAdapter` | `catalog_token` | `None`；注册表发现时自动注入 | 连接被替换后让旧 Schema 适配器快速失败 |

| 入口 | 操作 | 返回值 | 约束 |
| --- | --- | --- | --- |
| `McpServerRegistry` | `register/replace/unregister` | `None` | 新连接初始化成功后才可见；旧调用排空后关闭旧连接 |
| `McpServerRegistry` | `list_tools` | `tuple[McpToolDefinition, ...]` | cursor 不透明；限制页数、条目数和重复 cursor |
| `McpServerRegistry` | `discover_tools` | `tuple[McpToolAdapter, ...]` | 生成 `mcp__{namespace}__{tool}` 安全名称并检查碰撞 |
| `McpServerRegistry` | `call_tool` | `McpCallResult` | 单次调用固定连接租约；远端工具错误保留为 `is_error` |
| `McpServerRegistry` | `list_resources/read_resource` | 资源定义或内容 | 资源不会自动暴露为模型 Tool |
| `McpServerRegistry` | `list_resource_templates` | 资源模板元组 | 不自动展开 URI 模板 |
| `McpServerRegistry` | `list_prompts/get_prompt` | Prompt 定义或消息 | Prompt 不自动写入 Agent 的 developer/system 消息 |
| `McpServerRegistry` | `catalog` | `McpCatalog` | 在一个连接租约内顺序发现已声明能力；明确未声明的类别返回空元组 |
| `McpServerRegistry` | `aclose` | `None` | 禁止新操作，活跃操作完成后释放受托管连接 |

MCP 的控制语义保持分离：tools 可以经显式授权后交给模型调用；resources 由应用选择；prompts
由用户或控制面选择。本包不会把 resources 或 prompts 静默包装成模型工具，也不会自动响应
sampling、elicitation 或通知回调，这些能力由宿主构造 Session 时显式处理。

连接热替换会立即影响直接通过 Registry 发起的新操作；替换前已经开始的操作仍在旧连接租约内
完成。由 `discover_tools()` 生成的 Adapter 还绑定原目录令牌：连接替换后，旧 Adapter 的新调用
抛出 `McpCatalogStaleError`，宿主必须重新发现并在 `ToolRegistry` 中替换对应工具。这样不会用旧
JSON Schema 静默调用契约已经变化的新服务。

直接调用公共 `McpServerConnection` 时也有连接级活跃租约；`aclose()` 会先拒绝新操作，再等待
在途的 list/call/read/get/catalog 完整退出。Registry 在此基础上再提供跨连接注册与热替换租约。

### 官方 Python SDK v1 装配

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from matterloop_tools import (
    McpSdkV1SessionAdapter,
    McpServerConfig,
    McpServerConnection,
    McpServerRegistry,
    ToolRegistry,
)

params = StdioServerParameters(
    command="/opt/mcp/bin/company-server",
    args=["--stdio"],
    env={},  # 由宿主显式提供，不让 MatterLoop 读取进程环境
)

async with stdio_client(params) as (read_stream, write_stream):
    async with ClientSession(read_stream, write_stream) as session:
        adapter = McpSdkV1SessionAdapter(session)
        mcp_servers = McpServerRegistry()
        await mcp_servers.register(
            McpServerConnection(
                adapter,
                McpServerConfig(
                    name="company",
                    tool_namespace="company",
                    owns_session=False,
                ),
            )
        )

        remote_tools = await mcp_servers.discover_tools("company")
        tools = ToolRegistry(remote_tools, authorizer=company_authorizer)
        try:
            # 将 tools 交给 ToolCallingWorker；资源和 Prompt 通过 mcp_servers 显式访问。
            ...
        finally:
            await tools.aclose()
            await mcp_servers.aclose()
```

示例中的 `company_authorizer` 必须由应用提供。`McpSdkV1SessionAdapter` 构造时校验已安装版本为
`mcp>=1.28.1,<2`，并把中立 cursor 转为 v1 的 `PaginatedRequestParams`。它包装的是已经进入
异步上下文的 `ClientSession`；默认 `aclose()` 不退出原始 Session。若宿主显式传入
`close_callback`，回调只执行一次。MCP SDK v2 不在当前公共契约内，后续应使用独立适配器，
不能依赖 v1 适配器猜测版本。

### MCP 错误边界

| 异常 | 语义 |
| --- | --- |
| `McpConfigurationError` | 配置、SDK 版本或官方 Session 形态不兼容 |
| `McpLifecycleError` | 连接未启动、已关闭或 Adapter 已关闭 |
| `McpTimeoutError` | 初始化、请求或关闭超过本地硬时限 |
| `McpTransportError` | SDK/transport 失败；异常文本不包含原始供应商消息 |
| `McpProtocolError` | 响应缺字段或字段类型无法映射 |
| `McpRemoteError` | 远端协议级拒绝；只保留安全错误码 |
| `McpPaginationLimitError` | 页数、条目数或重复 cursor 触发边界 |
| `McpResponseLimitError` | 单次工具、资源或 Prompt 内容块超过本地边界 |
| `McpToolNameCollisionError` | 两个远端名称映射为相同的本地工具名 |
| `McpServerNotFoundError` | 服务名称未注册 |
| `McpCapabilityNotSupportedError` | 初始化结果明确未声明当前 tools/resources/prompts 能力 |
| `McpCatalogStaleError` | 连接已热替换，旧工具目录必须重新发现后才能调用 |

## Skills 集成

Skills 子系统安全加载专用根目录下一层的 `<name>/SKILL.md`。它不搜索 HOME、用户级配置、
Python 包入口点或环境变量，也不读取 `SKILL.md` 引用的其他文件。

```text
skills-root/
└── code-review/
    └── SKILL.md
```

支持的 frontmatter 是刻意受限的单行字符串子集：`name`、`description` 和 `version`。不支持
YAML 对象、锚点、标签、多行值或任意反序列化，因此不需要 PyYAML。

### Skill 公共字段

| 类型 | 字段 | 默认值与约束 | 安全与持久化 |
| --- | --- | --- | --- |
| `SkillLoaderConfig` | `root` | 必填且必须是现有目录 | 转为绝对路径；整条现存路径拒绝符号链接 |
| `SkillLoaderConfig` | `max_file_bytes` | `256_000`，正整数 | 读取前后都执行大小边界 |
| `SkillLoaderConfig` | `max_skills` | `128`，正整数 | 限制单次发现数量 |
| `SkillLoaderConfig` | `max_frontmatter_lines` | `32`，正整数 | 防止无界 frontmatter 扫描 |
| `SkillLoaderConfig` | `max_scan_entries` | `1_024`，正整数 | 在排序前限制根目录扫描的全部条目，包括无关文件 |
| `SkillSpec` | `name` | 1–64 个小写字母、数字、`-`、`_` | 必须等于直属目录名 |
| `SkillSpec` | `description` | 1–500 字符 | 来自受限 frontmatter 或正文推断 |
| `SkillSpec` | `source` | 必须为 `<name>/SKILL.md` | 仅保存相对来源，不泄露宿主绝对路径 |
| `SkillSpec` | `version` | `None`，最长 64 字符 | 不参与代码执行或依赖解析 |
| `SkillContent` | `spec/markdown/sha256` | 全部必填；摘要必须等于规范化 Markdown 的 UTF-8 SHA-256 | 用于审计和缓存，不代表来源可信 |
| `SkillAccessPolicy` | `allowed_names` | 必填；空集合全部拒绝 | Agent 只能发现和读取显式允许项 |
| `SkillAccessPolicy` | `max_content_chars` | `64_000`，正整数 | 限制一次注入模型上下文的字符数 |
| `SkillContextBlock` | `name/description/content/sha256/version` | 来自已加载内容 | 普通参考数据，不进入系统消息 |
| `SkillContextBlock` | `trust` | 固定 `UNTRUSTED_REFERENCE` | 提醒宿主防止权限提升与提示注入 |

### Skill 生命周期与 Tool 适配

| 构造器 | 参数 | 默认值与约束 |
| --- | --- | --- |
| `SkillLoader` | `config` | 必填 `SkillLoaderConfig` |
| `SkillRegistry` | `skills` | `()`，初始名称不得重复 |
| `SkillContextAdapter` | `registry/policy` | 均必填；策略不会因 registry 新增内容而自动扩权 |
| `SkillTool` | `adapter` | 必填 `SkillContextAdapter` |
| `SkillTool` | `name` | `"skill_reference"`，用于注册到 `ToolRegistry` |

| 入口 | 行为 | 并发与失败语义 |
| --- | --- | --- |
| `SkillLoader.discover/load` | 安全发现或加载 UTF-8 `SKILL.md` | 同步配置阶段 I/O；任一文档失败不返回部分目录 |
| `SkillRegistry.register/replace/unregister` | 更新不可变快照 | 读取方可继续使用旧值；新读取立即看到新值 |
| `SkillRegistry.refresh` | 从 Loader 全量刷新 | 锁外加载全部内容，成功后一次替换；失败保留旧快照 |
| `SkillRegistry.discover/get/names` | 读取当前快照 | 不执行磁盘 I/O |
| `SkillContextAdapter.discover/get_context` | 应用 allowlist 和字符上限 | 未授权、过大和不存在分别抛类型化异常 |
| `SkillTool` | `list/get` 两种只读操作 | 返回 JSON；没有 `run/execute/install` 操作 |

```python
from pathlib import Path

from matterloop_tools import (
    SkillAccessPolicy,
    SkillContextAdapter,
    SkillLoader,
    SkillLoaderConfig,
    SkillRegistry,
    SkillTool,
    ToolRegistry,
)

loader = SkillLoader(SkillLoaderConfig(root=Path("./company-skills")))
skills = SkillRegistry()
skills.refresh(loader)
skill_tool = SkillTool(
    SkillContextAdapter(
        skills,
        SkillAccessPolicy.from_names({"code-review"}),
    )
)
tools = ToolRegistry((skill_tool,), authorizer=company_authorizer)
```

`SkillTool` 只返回带 `untrusted_reference` 标签的目录或正文，不解析代码块、不执行命令，也不
递归读取引用文件。只有来源经过组织审核、版本固定且允许用于当前租户的 Skill 才应进入
allowlist；Skill 正文仍可能包含提示注入、敏感数据或危险建议。加载器拒绝 Skill 路径中的
符号链接、`SKILL.md` 硬链接和打开期间的 inode 替换，并同时限制文件字节、目录项、Skill 数量
和 frontmatter 行数；专用根目录仍应只允许受信任的发布流程写入。

## `FileSystemTool`

### 构造参数

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `root` | 必填 | 必须是已存在目录；按字面值解析，不展开 `~` |
| `allow_write` | `False` | 仅显式开启后允许 `write` |
| `max_read_bytes` | `1_000_000` | 至少为 1 |
| `max_write_bytes` | `1_000_000` | 至少为 1 |
| `max_list_entries` | `1_000` | 至少为 1 |

工具名固定为 `filesystem`。

### 调用参数

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `operation` | 是 | 无 | `read`、`list`、`exists`、`stat`、`write` |
| `path` | 是 | 无 | 绝对或相对路径；最终必须位于 root 内 |
| `content` | `write` 时是 | 无 | UTF-8 文本；允许空字符串 |

### 返回

- `read`：UTF-8 文本；非文件、超限或无效 UTF-8 会失败；
- `list`：按名称排序的 JSON 数组，metadata 包含 `truncated`；
- `exists`：`{"exists": bool}` JSON；
- `stat`：包含 `size`、`modified_ns`、`is_file`、`is_directory` 的 JSON；
- `write`：原子替换后返回 path 与 `bytes_written`。

路径在词法层和 `resolve(strict=False)` 后均检查是否逃逸，并逐级拒绝符号链接；实际 I/O 前会重复检查。写入使用同目录临时文件、`fsync` 和 `os.replace`，目标已存在时继承其模式。

这些检查能阻止常见 `..`、绝对路径和符号链接逃逸，但不能消除同一主机恶意进程并发替换目录产生的 TOCTOU 风险，也不限制硬链接、文件权限主体、磁盘配额或挂载变化。强对抗场景应使用容器、独立文件服务或内核级沙箱。

## `ShellTool`

### 构造参数

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `workspace` | 必填 | 必须是已存在目录；不展开 `~` |
| `allowed_commands` | 必填 | 非空裸程序名集合，例如 `{"pytest", "ruff"}` |
| `sandbox` | `None` | 默认 `LocalProcessSandbox(workspace, base_environment=...)` |
| `base_environment` | `None` | 默认沙箱的显式基础环境；与自定义 sandbox 互斥 |
| `allowed_environment` | `frozenset()` | 调用参数允许覆盖的环境变量名 |
| `max_timeout_seconds` | `60.0` | 必须大于 0 |
| `max_output_bytes` | `1_000_000` | stdout/stderr 共享硬上限，至少为 1 |

工具名固定为 `shell`。实现直接传递 argv，不使用 `shell=True`，不解释管道、重定向、变量替换或命令替换。

### 调用参数

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `argv` | 是 | 无 | 非空字符串数组；元素不能含 NUL；第一个元素必须是白名单裸程序名 |
| `cwd` | 否 | `"."` | 由 Sandbox 在 workspace 内校验 |
| `environment` | 否 | `{}` | 键和值必须为字符串，键必须在 `allowed_environment` |
| `stdin` | 否 | `None` | 文本，执行前编码为 UTF-8 |
| `timeout_seconds` | 否 | 配置上限 | 必须为正数且不得超过配置上限 |

返回内容是 JSON：`return_code`、`stdout`、`stderr`、`timed_out`、`truncated`。非零退出码或超时会设置 `ToolResult.is_error=True`；metadata 还包含 `duration_seconds`。

默认进程环境不继承宿主环境。若需要按名称查找命令，必须在 `base_environment` 中显式提供受控 `PATH`：

```python
from matterloop_tools import ShellTool

shell = ShellTool(
    "./workspace",
    allowed_commands={"pytest", "ruff"},
    base_environment={"PATH": "/opt/matterloop/bin:/usr/bin"},
)
```

程序名白名单不等于参数安全。`python -c`、测试插件、编译器参数、包管理器脚本等仍可能执行任意代码或访问网络。应同时限制可执行程序、argv 形状、cwd、环境和调用身份，并在高风险场景替换为真正隔离的 Sandbox。`LocalProcessSandbox` 只提供 cwd、环境、超时和输出边界，不承诺恶意代码隔离。

## `HttpTool`

### 构造参数

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `allowed_hosts` | 必填 | 非空、精确主机名集合；去尾点、转小写并做 IDNA 规范化，不含端口 |
| `allowed_methods` | `{"GET"}` | 非空，统一转大写 |
| `require_https` | `True` | 启用时只允许 HTTPS；关闭时仅允许 HTTP/HTTPS |
| `follow_redirects` | `False` | 开启后由工具手动跟随并逐跳复核 URL |
| `max_redirects` | `3` | 可为 0，不得为负数 |
| `max_timeout_seconds` | `20.0` | 大于 0 |
| `max_response_bytes` | `2_000_000` | 大于 0 |
| `max_request_bytes` | `1_000_000` | 大于 0 |
| `allowed_headers` | `{"accept", "content-type", "user-agent"}` | 统一转小写 |
| `transport` | `None` | 测试或调用方显式网络配置使用的 httpx transport |

工具名固定为 `http`。内部 `httpx.AsyncClient` 固定 `follow_redirects=False`、`trust_env=False`，不会读取代理、`NO_PROXY` 或证书环境变量。需要私有 CA、代理、DNS 或连接策略时，通过 transport 显式配置。

### 调用参数

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `url` | 是 | 无 | 绝对 URL；禁止 userinfo 凭据；协议与 host 必须允许 |
| `method` | 否 | `"GET"` | 转大写后必须在方法白名单 |
| `headers` | 否 | `{}` | 仅允许白名单头；值禁止 CR/LF |
| `body` | 否 | `None` | UTF-8 文本且不超过请求体上限 |
| `timeout_seconds` | 否 | 配置上限 | 正数且不得超过上限 |

返回响应文本；未知编码会回退 UTF-8 并替换非法字节。`ToolResult.is_error` 等于 httpx 的 `response.is_error`，metadata 包含 `status_code`、`final_url`、`content_type`、`redirects` 和 `truncated`。

开启重定向后，每一跳都会重新检查协议、userinfo 和主机；303 以及 POST 的 301/302 会转换为 GET，并再次核对允许方法。不开启重定向时直接返回 3xx 响应。

主机白名单只校验规范化 DNS 名，不固定解析后的 IP，也不阻止 DNS rebinding、私网解析或允许主机上的任意端口。需要 SSRF 强隔离时，调用方 transport 或外部网络策略还必须限制解析结果、CIDR、端口、出口和 TLS 身份。默认请求头不包含 Authorization/Cookie；若显式放开这些头，凭据保护责任由调用方承担。

`HttpTool` 持有异步客户端，必须直接调用 `aclose()`，或让 `ToolRegistry` 在注销、热替换和关闭时管理它。

## 错误分类

| 异常 | 触发场景 |
| --- | --- |
| `ToolConfigurationError` | 构造配置缺失、不安全或资源上限非法 |
| `ToolInputError` | 参数类型错误、操作不支持、路径/命令/URL/资源边界被拒绝 |
| `ToolPermissionDeniedError` | 授权器未返回 ALLOW |
| `ToolNotFoundError` | 注册表中不存在工具 |
| `ToolError` | 本包工具异常基类 |

底层 Sandbox、httpx transport、自定义工具和生命周期方法的异常不一定会转换为 `ToolError`。调用方应按边界设置超时并分类处理，不要在外部错误响应中直接返回底层异常文本。

## 敏感数据与审计边界

- 工具参数、stdout/stderr、文件内容、HTTP 响应和 `ToolResult.metadata` 可能包含敏感数据；本包不会自动脱敏或截断日志。
- `ToolContext.metadata` 应只放授权和关联所需数据。若包含租户或主体信息，授权器必须验证其来源，不能信任模型生成的 metadata。
- 文件和 HTTP 内容会返回给 Agent，必须防范提示注入；外部内容不能改变工具白名单、审批策略或系统指令。
- Shell 环境变量可能包含凭据。即使显式允许，也不应通过模型参数传入密钥；优先在隔离运行环境中注入短期凭据。
- `ToolSpec.description` 和 Schema 会进入模型上下文，不要在其中放置内部秘密、真实路径或访问令牌。
- 需要调用计数和费用边界时，在注册前用 `BudgetedTool` 包装，或在自定义授权器中接入审计系统。

## 当前限制

- 没有通用 JSON Schema 执行器；自定义工具负责本地参数校验。
- MCP SDK v1 桥接只包装宿主已进入上下文的 `ClientSession`；不构造 stdio/HTTP transport、OAuth 或自动重连。
- MCP 的页项与内容块检查发生在 SDK 返回对象之后、Mapper 再次物化 DTO 之前；宿主仍必须在 transport/反向代理层限制入站响应体，不能把这些字段当作网络内存硬隔离。
- MCP 目录不缓存，也不消费 `list_changed` 通知；连接替换后旧 Adapter 会快速失败，需要由宿主重新发现并替换对应工具。
- 不实现 MCP sampling、elicitation、completion 或 task 扩展；这些回调与权限必须由宿主显式配置。
- Skills 只读取直属 `SKILL.md`，不解析依赖、引用文件、脚本、插件入口点或远端 Skill 仓库。
- Skills 的路径与 inode 检查不是同机恶意主体隔离边界；生产根目录仍需文件权限、只读挂载和发布审计。
- 没有身份认证、RBAC/ABAC、人工审批 UI 或持久化审计实现。
- `FileSystemTool` 只处理 UTF-8 文本，不提供二进制、删除、移动、目录创建或 glob。
- `ShellTool` 不承诺恶意代码隔离，也不解析命令语义来限制危险参数。
- `HttpTool` 不提供 DNS/IP/CIDR/端口钉扎、响应 MIME 白名单、证书指纹钉扎或身份认证助手。
- 内置工具均是单次调用组件，不提供事务回滚；外部副作用必须由业务实现保证幂等。
