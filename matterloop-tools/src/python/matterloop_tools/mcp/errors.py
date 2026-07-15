"""MCP 连接、协议映射与工具适配异常。"""

from __future__ import annotations

from matterloop_tools.errors import ToolError


class McpError(ToolError):
    """所有 MCP 子系统异常的基类。"""


class McpConfigurationError(McpError):
    """MCP 连接或资源限制配置不合法。"""


class McpLifecycleError(McpError):
    """MCP 连接尚未启动、已关闭或生命周期操作无效。"""


class McpServerNotFoundError(McpError):
    """请求的 MCP 服务连接未注册。"""

    def __init__(self, server_name: str) -> None:
        """初始化异常。

        Args:
            server_name: 未找到的 MCP 服务注册名称。
        """
        super().__init__(f"MCP server not found: {server_name}")


class McpCapabilityNotSupportedError(McpError):
    """初始化结果明确未声明请求的 MCP 服务端能力。"""

    def __init__(self, server_name: str, capability: str) -> None:
        """初始化能力不支持异常。

        Args:
            server_name: MCP 服务注册名称。
            capability: 请求的标准能力名称。
        """
        super().__init__(f"MCP server {server_name} does not support capability: {capability}")


class McpCatalogStaleError(McpError):
    """工具适配器绑定的发现目录已被连接热替换淘汰。"""

    def __init__(self, server_name: str, tool_name: str) -> None:
        """初始化目录过期异常。

        Args:
            server_name: MCP 服务注册名称。
            tool_name: 过期目录中的远端工具名称。
        """
        del tool_name
        super().__init__(f"MCP tool catalog is stale for server {server_name}; rediscover tools")


class McpTimeoutError(McpError):
    """MCP 操作超过调用方配置的本地时限。"""

    def __init__(self, operation: str, timeout_seconds: float) -> None:
        """初始化不包含供应商数据的超时异常。

        Args:
            operation: 标准化操作名称。
            timeout_seconds: 本地超时秒数。
        """
        super().__init__(f"MCP operation timed out: {operation} ({timeout_seconds:g}s)")


class McpTransportError(McpError):
    """Session Adapter 在传输阶段失败。

    异常消息不会拼接原始 SDK 异常，避免请求头、凭据或服务端自由文本泄漏。
    """

    def __init__(self, operation: str) -> None:
        """初始化安全的传输异常。

        Args:
            operation: 标准化操作名称。
        """
        super().__init__(f"MCP transport failed: {operation}")


class McpProtocolError(McpError):
    """MCP 响应结构无法映射到稳定 DTO。"""

    def __init__(self, operation: str, detail: str) -> None:
        """初始化协议映射异常。

        Args:
            operation: 标准化操作名称。
            detail: 不包含原始载荷的结构错误说明。
        """
        super().__init__(f"invalid MCP response for {operation}: {detail}")


class McpRemoteError(McpError):
    """远端返回了协议级错误，而不是工具级错误结果。"""

    def __init__(self, operation: str, code: str | int | None = None) -> None:
        """初始化经过脱敏的远端错误。

        Args:
            operation: 标准化操作名称。
            code: 可安全记录的错误码；远端自由文本不会进入异常。
        """
        suffix = "" if code is None else f" (code={code})"
        super().__init__(f"MCP remote rejected operation: {operation}{suffix}")


class McpPaginationLimitError(McpError):
    """分页数量、条目数量或游标安全边界被触发。"""

    def __init__(self, operation: str, reason: str) -> None:
        """初始化分页限制异常。

        Args:
            operation: 标准化列表操作名称。
            reason: 固定的限制原因，不得包含远端载荷。
        """
        super().__init__(f"MCP pagination limit exceeded for {operation}: {reason}")


class McpResponseLimitError(McpError):
    """单次 MCP 响应的内容块数量超过本地边界。"""

    def __init__(self, operation: str, reason: str) -> None:
        """初始化不包含远端载荷的响应限制异常。

        Args:
            operation: 标准化操作名称。
            reason: 固定的本地限制名称。
        """
        super().__init__(f"MCP response limit exceeded for {operation}: {reason}")


class McpToolNameCollisionError(McpError):
    """两个远端工具被规范化为相同的本地安全名称。"""

    def __init__(self, exposed_name: str) -> None:
        """初始化名称冲突异常。

        Args:
            exposed_name: 发生冲突的安全本地名称。
        """
        super().__init__(f"MCP tool name collision: {exposed_name}")
