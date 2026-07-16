[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-tools/README.md) | English

# matterloop-tools

Tools are the permission boundary between an Agent and the outside world. This package provides a
unified invocation protocol, a hot-swappable registry, MCP and Skill adapters, and three constrained
implementations for files, processes, and HTTP.

```bash
pip install matterloop-tools
# When bridging through the official MCP Python SDK v1
pip install "matterloop-tools[mcp]"
```

## Every call goes through one entry point

```text
Agent
  └─ ToolRegistry.invoke(name, arguments, context)
       ├─ Pins the tool instance for this call
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

When `ToolRegistry(tools, authorizer)` receives no authorizer, it uses
`AllowAllToolAuthorizer`. That is convenient for tests but is not an appropriate production default.
The authorizer should decide from trusted identity, tenant, tool name, arguments, and `ToolContext`.

## Tool protocol and lifecycle

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

- `ToolSpec(name, description, input_schema)` describes the model-visible invocation interface.
- `ToolContext(run_id, step_id, metadata)` carries authorization and correlation data. `metadata`
  accepts only JSON-compatible values and is recursively snapshotted before invocation.
- `ToolResult(content, is_error, metadata)` returns text output and safe diagnostics.

The Schema is for discovery; it does not mean that the registry automatically evaluates JSON Schema.
Every custom tool must validate all arguments locally. Provider-side strict tools do not replace
authorization.

`register(tool, replace=False)`, `replace(name, tool)`, `unregister(name)`, and `aclose()` manage the
lifecycle. A new implementation replaces the old one only after it starts successfully. Calls already
in progress continue using the old instance, which closes after its final call exits. The tool lease
covers both authorization and execution, preventing a different implementation from being swapped in
between those stages. If closing the old component fails, the new component may already be committed;
the caller should inspect registry state instead of blindly retrying replacement.

## MCP: the host establishes connections

MatterLoop does not create stdio subprocesses, HTTP transports, OAuth clients, or Sessions. The
application first establishes and enters the connection context, then injects a minimal Session
adapter:

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

`McpServerConnection` accepts `session`, `config`, and an optional keyword-only `mapper`. When mapper
is null, it uses the default response mapper.

`McpSdkV1SessionAdapter(session, close_callback)` only bridges an official SDK v1 Session that has
already entered its context; by default, it does not exit the host Session. Endpoints, request headers,
certificates, proxies, credentials, and the process environment always belong to the composition
root.

### Capabilities remain separate

- Tools can become `McpToolAdapter` instances through `discover_tools()` and then enter the
  `ToolRegistry` authorization path.
- Resources are accessed through `list_resources/read_resource`; they are not automatically
  presented as model tools.
- Prompts are obtained through `list_prompts/get_prompt`; they are not automatically promoted to
  system/developer instructions.
- Resource templates are listed only; URIs are not filled automatically.

`catalog()` reads the complete catalog under one connection lease. After a hot replacement, tool
adapters from the old catalog raise `McpCatalogStaleError`. The host must rediscover and replace the
tools so an old Schema cannot invoke a new service.

<details>
<summary>MCP data structure and limit reference</summary>

- `McpLimits(request_timeout_seconds, initialize_timeout_seconds, close_timeout_seconds, max_pages, max_items, max_content_blocks, max_result_characters)`: defaults to 30/15/10 seconds, 20 pages, 1,000 items, 256 content blocks, and 200,000 characters.
- `McpServerConfig(name, tool_namespace, limits, initialize_on_start, owns_session)`: service identity, local tool namespace, and Session ownership.
- `McpServerCapabilities(tools, resources, prompts, completions, logging)`: `True/False/None` means declared support, explicit lack of support, or not yet negotiated, respectively.
- `McpToolDefinition(name, description, input_schema, output_schema, annotations)`.
- `McpResourceDefinition(uri, name, description, mime_type, size, metadata)`.
- `McpResourceTemplateDefinition(uri_template, name, description, mime_type, metadata)`.
- `McpPromptArgument(name, description, required)` and `McpPromptDefinition(name, description, arguments)`.
- `McpContent(kind, text, data, mime_type, uri, metadata)`: unified text, JSON, image, audio, resource, and binary content block.
- `McpCallResult(content, structured_content, is_error, metadata)` and `McpResourceResult(contents, metadata)`.
- `McpPromptMessage(role, content)` and `McpPromptResult(messages, description, metadata)`.
- `McpCatalog(tools, resources, resource_templates, prompts)`: complete catalog snapshot under one lease.

</details>

Page, item, and content-block limits reject anomalous responses early, but the SDK or transport may
already have materialized the raw response in memory. These limits are not hard isolation for a
network body. A reverse proxy or custom transport must still cap response bodies, connections, and
download sizes.

MCP failures are normalized into typed errors for configuration, lifecycle, timeout, transport,
protocol, remote rejection, pagination/response limits, missing capabilities, and stale catalogs.
Security-oriented exceptions do not concatenate raw remote error text. This package currently does
not implement sampling, elicitation, completion, task extensions, automatic reconnection, or
`list_changed` notifications.

## Skills: read-only references, not code plugins

The Skill loader scans only the first `<name>/SKILL.md` level under a dedicated root:

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

`SkillTool` has only the `list/get` operations. It does not execute code blocks, install
dependencies, run commands, or recursively read referenced files. Returned content carries the
`UNTRUSTED_REFERENCE` trust label. A Skill body may still contain prompt injection or dangerous
advice.

<details>
<summary>Skill data structure reference</summary>

- `SkillLoaderConfig(root, max_file_bytes, max_skills, max_frontmatter_lines, max_scan_entries)`: defaults to limits of 256,000 bytes, 128 Skills, 32 frontmatter lines, and 1,024 directory entries.
- `SkillSpec(name, description, source, version)`: the name must match its immediate directory; source stores only a relative path.
- `SkillContent(spec, markdown, sha256)`: normalized body and content digest.
- `SkillAccessPolicy(allowed_names, max_content_chars)`: explicit allowlist, with at most 64,000 characters per call by default.
- `SkillContextBlock(name, description, content, sha256, trust, version)`: read-only reference block passed to the model.

Construction entry points are `SkillLoader(config)`, `SkillRegistry(skills)`,
`SkillContextAdapter(registry, policy)`, and `SkillTool(adapter, name)`.

</details>

The loader rejects path symlinks, hard-linked `SKILL.md` files, and inode replacement while a file is
being opened. It also limits directory scans and file size. These measures cannot isolate a malicious
principal that has write permission on the same host. Mount production roots read-only and maintain
them through a trusted release process.

## Actual boundaries of the built-in tools

### FileSystemTool

`FileSystemTool(root, allow_write, max_read_bytes, max_write_bytes, max_list_entries)` is read-only by
default and supports `read/list/exists/stat/write`. Paths undergo lexical, resolved-path, and
component-by-component symlink checks. Writes use a temporary file in the same directory followed by
an atomic replacement.

It cannot eliminate TOCTOU, hard-link, or mount-change attacks from a malicious process on the same
host. It also does not support binary data, deletion, moving, `mkdir`, or globbing. Use an isolated
file service in adversarial environments.

### ShellTool

`ShellTool(workspace, allowed_commands, sandbox, base_environment, allowed_environment, max_timeout_seconds, max_output_bytes)`
accepts argv only and never uses `shell=True`. A command must be a bare program name in the allowlist.
The default environment is empty, and stdout/stderr share one output budget.

A program-name allowlist is not argument safety: `python -c`, test plugins, compilers, and package
managers can still execute arbitrary code. `LocalProcessSandbox` limits only cwd, environment,
timeout, and output; it is not a malicious-code isolation boundary.

### HttpTool

`HttpTool(allowed_hosts, allowed_methods, require_https, follow_redirects, max_redirects, max_timeout_seconds, max_response_bytes, max_request_bytes, allowed_headers, transport)`
allows only HTTPS `GET` and exact host allowlisting by default and does not inherit system proxies.
When redirects are enabled, every hop is revalidated.

Host validation does not pin DNS results, prevent rebinding or private-network resolution, or forbid
arbitrary ports on an allowed host. A strong SSRF boundary must additionally restrict CIDRs, ports,
egress, and TLS identity in the transport or network layer.

## Errors, auditing, and shutdown

Configuration errors, invalid input, denied authorization, and missing names use
`ToolConfigurationError`, `ToolInputError`, `ToolPermissionDeniedError`, and `ToolNotFoundError`,
respectively. Lower-level transport, Sandbox, and lifecycle exceptions are not necessarily converted
into `ToolError`; do not return raw exception text to external callers.

Arguments, file contents, stdout/stderr, HTTP/MCP responses, and Tool metadata may all be sensitive.
This package does not automatically redact logs. External content must also be treated as an
untrusted reference and cannot change approval or permission policy. Wrap a tool with `BudgetedTool`
before registration when invocation quotas are required. See the
[enterprise integration guide](https://github.com/huleidada/matterloop/blob/main/docs/enterprise-integration.en.md)
for production composition and resource shutdown order.
