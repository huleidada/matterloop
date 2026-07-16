# matterloop-tools

工具是 Agent 与外部世界之间的权限边界。这个包提供统一调用协议、热替换注册表、MCP 与 Skill
适配，以及文件、进程和 HTTP 三个受限实现。

```bash
pip install matterloop-tools
# 使用官方 MCP Python SDK v1 桥接时
pip install "matterloop-tools[mcp]"
```

## 所有调用都经过一个入口

```text
Agent
  └─ ToolRegistry.invoke(name, arguments, context)
       ├─ 固定本次调用使用的工具实例
       ├─ ToolAuthorizer.authorize(...)
       └─ Tool.invoke(...)
            ├─ FileSystemTool / ShellTool / HttpTool
            ├─ McpToolAdapter → McpServerRegistry → injected Session
            └─ SkillTool → SkillContextAdapter → SkillRegistry
```

```python
from matterloop_policies import PermissionRule, RuleBasedPermissionPolicy
from matterloop_tools import FileSystemTool, PermissionDecision, ToolRegistry

authorizer = RuleBasedPermissionPolicy(
    rules=(
        PermissionRule(
            tool="filesystem",
            operations=("read", "list", "exists", "stat"),
            decision=PermissionDecision.ALLOW,
        ),
    )
)
tools = ToolRegistry(
    tools=(FileSystemTool("./workspace"),),
    authorizer=authorizer,
)
```

`ToolRegistry(tools, authorizer)` 在没有 authorizer 时使用 `AllowAllToolAuthorizer`。这方便测试，但
不适合作为生产默认。授权器应根据可信身份、租户、工具名、参数与 `ToolContext` 作出决定。

## 工具协议与生命周期

```python
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def invoke(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult: ...
```

- `ToolSpec(name, description, input_schema)` 描述模型可见的调用接口。
- `ToolContext(run_id, step_id, metadata)` 携带授权与关联信息；metadata 只接受 JSON-compatible 值，
  并在调用前递归快照。
- `ToolResult(content, is_error, metadata)` 返回文本结果和安全诊断。

Schema 用于发现，不代表注册表会自动执行 JSON Schema。自定义工具必须在本地校验全部参数；模型
侧的 strict tools 也不能替代授权。

`register(tool, replace=False)`、`replace(name, tool)`、`unregister(name)` 和 `aclose()` 负责生命周期。
新实现启动成功后才会替换；已经开始的调用继续使用旧实例，最后一个调用退出后旧实例才关闭。
工具租约同时覆盖授权与执行，避免授权后被换成不同实现。旧组件关闭失败时，新组件可能已经提交，
调用方应查询注册表状态而不是盲目重试替换。

## MCP：连接由宿主建立

MatterLoop 不创建 stdio 子进程、HTTP transport、OAuth client 或 Session。应用先建立并进入连接
上下文，再把最小 Session adapter 注入：

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
    env={},
)

async with stdio_client(params) as streams:
    async with ClientSession(*streams) as session:
        servers = McpServerRegistry()
        await servers.register(
            McpServerConnection(
                session=McpSdkV1SessionAdapter(session),
                config=McpServerConfig(
                    name="company",
                    tool_namespace="company",
                    owns_session=False,
                ),
                mapper=None,
            )
        )
        remote_tools = await servers.discover_tools("company")
        tools = ToolRegistry(remote_tools, authorizer=authorizer)
```

`McpServerConnection` 接收 `session`、`config` 和可选的 keyword-only `mapper`；mapper 为空时使用默认
响应映射器。

`McpSdkV1SessionAdapter(session, close_callback)` 只桥接已进入上下文的官方 SDK v1 Session；默认不会
退出宿主 Session。端点、请求头、证书、代理、凭据和进程环境始终属于组合根。

### 能力保持分离

- tools 可以通过 `discover_tools()` 变成 `McpToolAdapter`，再进入 `ToolRegistry` 的授权链路。
- resources 通过 `list_resources/read_resource` 访问，不会自动伪装成模型工具。
- prompts 通过 `list_prompts/get_prompt` 获取，不会自动提升为 system/developer 指令。
- resource templates 只被列出，不自动填充 URI。

`catalog()` 在一个连接租约内读取完整目录。连接热替换后，旧目录产生的工具适配器会抛
`McpCatalogStaleError`，宿主需要重新发现并替换工具，避免旧 Schema 调用新服务。

<details>
<summary>MCP 数据结构与限制速查</summary>

- `McpLimits(request_timeout_seconds, initialize_timeout_seconds, close_timeout_seconds, max_pages, max_items, max_content_blocks, max_result_characters)`：默认 30/15/10 秒、20 页、1,000 项、256 个内容块和 200,000 字符。
- `McpServerConfig(name, tool_namespace, limits, initialize_on_start, owns_session)`：服务标识、本地工具命名空间和 Session 所有权。
- `McpServerCapabilities(tools, resources, prompts, completions, logging)`：`True/False/None` 分别表示声明支持、明确不支持和尚未协商。
- `McpToolDefinition(name, description, input_schema, output_schema, annotations)`。
- `McpResourceDefinition(uri, name, description, mime_type, size, metadata)`。
- `McpResourceTemplateDefinition(uri_template, name, description, mime_type, metadata)`。
- `McpPromptArgument(name, description, required)` 与 `McpPromptDefinition(name, description, arguments)`。
- `McpContent(kind, text, data, mime_type, uri, metadata)`：统一文本、JSON、图片、音频、资源与二进制内容块。
- `McpCallResult(content, structured_content, is_error, metadata)`、`McpResourceResult(contents, metadata)`。
- `McpPromptMessage(role, content)`、`McpPromptResult(messages, description, metadata)`。
- `McpCatalog(tools, resources, resource_templates, prompts)`：单次租约中的完整目录快照。

</details>

页数、项目数和内容块限制用于尽早拒绝异常响应，但 SDK/transport 可能已经在内存中物化原始响应。
它们不是网络 body 的硬隔离。反向代理或自定义 transport 仍需限制响应体、连接和下载大小。

MCP 失败会归一化为配置、生命周期、超时、transport、协议、远端拒绝、分页/响应限额、能力缺失
和陈旧目录等类型化异常。安全异常不拼接远端原始错误文本。本包当前不处理 sampling、elicitation、
completion、task 扩展、自动重连或 `list_changed` 通知。

## Skills：只读参考，不是代码插件

Skill loader 只扫描专用根目录的下一层 `<name>/SKILL.md`：

```text
company-skills/
└── code-review/
    └── SKILL.md
```

```python
from pathlib import Path

from matterloop_tools import (
    SkillAccessPolicy,
    SkillContextAdapter,
    SkillLoader,
    SkillLoaderConfig,
    SkillRegistry,
    SkillTool,
)

loader = SkillLoader(SkillLoaderConfig(root=Path("./company-skills")))
skills = SkillRegistry(skills=())
skills.refresh(loader)
adapter = SkillContextAdapter(
    registry=skills,
    policy=SkillAccessPolicy.from_names({"code-review"}),
)
skill_tool = SkillTool(adapter=adapter, name="skill_reference")
```

`SkillTool` 只有 `list/get` 两个操作。它不会执行代码块、安装依赖、运行命令或递归读取引用文件。
返回内容带 `UNTRUSTED_REFERENCE` 信任标记；Skill 正文仍可能包含提示注入或危险建议。

<details>
<summary>Skill 数据结构速查</summary>

- `SkillLoaderConfig(root, max_file_bytes, max_skills, max_frontmatter_lines, max_scan_entries)`：默认限制 256,000 字节、128 个 Skill、32 行 frontmatter 和 1,024 个目录项。
- `SkillSpec(name, description, source, version)`：名称必须与直属目录一致，source 只保存相对路径。
- `SkillContent(spec, markdown, sha256)`：规范化正文与内容摘要。
- `SkillAccessPolicy(allowed_names, max_content_chars)`：显式 allowlist，默认单次最多 64,000 字符。
- `SkillContextBlock(name, description, content, sha256, trust, version)`：交给模型的只读参考块。

构造入口为 `SkillLoader(config)`、`SkillRegistry(skills)`、`SkillContextAdapter(registry, policy)` 和
`SkillTool(adapter, name)`。

</details>

加载器拒绝路径符号链接、`SKILL.md` 硬链接和打开期间的 inode 替换，并限制扫描与文件大小。
这些措施不能隔离拥有同机写权限的恶意主体；生产根目录应只读挂载，并由受信任发布流程维护。

## 内置工具的实际边界

### FileSystemTool

`FileSystemTool(root, allow_write, max_read_bytes, max_write_bytes, max_list_entries)` 默认只读，支持
`read/list/exists/stat/write`。路径会做词法、resolve 和逐级符号链接检查；写入使用同目录临时文件
与原子替换。

它不能消除同机恶意进程的 TOCTOU、硬链接或挂载变化，也不支持二进制、删除、移动、mkdir 和
glob。高对抗场景应使用隔离文件服务。

### ShellTool

`ShellTool(workspace, allowed_commands, sandbox, base_environment, allowed_environment, max_timeout_seconds, max_output_bytes)` 只接受 argv，不使用 `shell=True`。命令必须是白名单中的裸程序名；默认环境为空，
stdout/stderr 共享输出预算。

程序名白名单不等于参数安全：`python -c`、测试插件、编译器和包管理器仍可能执行任意代码。
`LocalProcessSandbox` 也只限制 cwd、环境、超时和输出，不是恶意代码隔离。

### HttpTool

`HttpTool(allowed_hosts, allowed_methods, require_https, follow_redirects, max_redirects, max_timeout_seconds, max_response_bytes, max_request_bytes, allowed_headers, transport)` 默认只允许 HTTPS `GET` 和精确 host allowlist，
不继承系统代理；重定向开启后逐跳复核 URL。

host 校验不固定 DNS 解析结果，也不阻止 rebinding、私网解析或允许主机上的任意端口。强 SSRF
边界需要在 transport 或网络层继续限制 CIDR、端口、出口与 TLS 身份。

## 错误、审计与关闭

配置错误、输入错误、拒绝授权和名称缺失分别使用 `ToolConfigurationError`、`ToolInputError`、
`ToolPermissionDeniedError` 与 `ToolNotFoundError`。底层 transport、Sandbox 和生命周期异常不一定
转换成 `ToolError`，不要把原始异常文本直接返回给外部调用者。

参数、文件内容、stdout/stderr、HTTP/MCP 响应和 Tool metadata 都可能敏感；本包不自动脱敏日志。
外部内容也应被视为不可信参考，不能改变审批或权限策略。需要调用次数预算时，在注册前使用
`BudgetedTool` 包装。生产组合与资源关闭顺序见[企业集成指南](../docs/enterprise-integration.md)。
