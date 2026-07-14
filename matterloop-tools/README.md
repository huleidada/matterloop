# matterloop-tools

> 跨模块生产拓扑、资源所有权与上线检查见[企业集成指南](../docs/enterprise-integration.md)。

`matterloop-tools` 提供供应商无关的异步工具协议、统一注册与授权入口，以及受边界约束的文件、进程和 HTTP 工具。

工具不会读取 `.env` 或自动继承宿主环境。内置工具以减少误用和路径/协议逃逸为目标，但不是恶意代码、恶意同机进程或不可信网络的安全隔离边界。

## 企业装配原则

```text
Agent / Worker
      │ name + arguments + ToolContext
      ▼
ToolRegistry
      ├─ ToolAuthorizer.authorize(...)
      ├─ RuntimeContainer.acquire(name)
      └─ Tool.invoke(...)
             ├─ FileSystemTool
             ├─ ShellTool -> Sandbox
             └─ HttpTool -> httpx transport
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
| 异常 | `ToolError`、`ToolNotFoundError`、`ToolPermissionDeniedError`、`ToolInputError`、`ToolConfigurationError` |

## 公共 DTO 与协议

### DTO 字段

| 类型 | 字段 | 默认值与约束 |
| --- | --- | --- |
| `ToolSpec` | `name`、`description`、`input_schema` | 全部必填；名称和描述不得为空；Schema 顶层只读 |
| `ToolContext` | `run_id`、`step_id`、`metadata` | `run_id` 必填非空；`step_id=None`；`metadata={}` 且顶层只读 |
| `ToolResult` | `content`、`is_error`、`metadata` | `content` 必填；`is_error=False`；`metadata={}` 且顶层只读 |
| `PermissionDecision` | `ALLOW`、`DENY` | 授权器必须返回枚举值；任何非 `ALLOW` 结果都被注册表拒绝 |

映射只冻结顶层，嵌套值仍应被视为不可变。`ToolResult.content`、metadata 和 `ToolContext.metadata` 不会自动脱敏。

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
| `invoke` | `name, arguments, context=` | 先授权，再借用调用期固定实例并执行 |
| `aclose` | 无 | 阻止新调用；立即关闭空闲工具，活跃工具在调用退出后关闭 |

热替换由 `RuntimeContainer` 管理：新实例只有在 `start()` 成功后才原子换入；启动失败会尽力关闭新实例并保留旧实例。已经开始的调用继续使用旧工具，最后一个旧调用结束后才关闭旧实例。

授权发生在获取工具之前。授权器应避免耗时外部调用，且必须根据调用方身份、租户、工具名、具体参数和运行上下文作出决定。注册表关闭后，Runtime 的 `RuntimeClosedError` 可能直接向上传播；它不是 `ToolError` 子类。

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
- 没有身份认证、RBAC/ABAC、人工审批 UI 或持久化审计实现。
- `FileSystemTool` 只处理 UTF-8 文本，不提供二进制、删除、移动、目录创建或 glob。
- `ShellTool` 不承诺恶意代码隔离，也不解析命令语义来限制危险参数。
- `HttpTool` 不提供 DNS/IP/CIDR/端口钉扎、响应 MIME 白名单、证书指纹钉扎或身份认证助手。
- 内置工具均是单次调用组件，不提供事务回滚；外部副作用必须由业务实现保证幂等。
