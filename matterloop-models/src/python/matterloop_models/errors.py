"""定义模型模块可由上层分类处理的异常。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matterloop_models.base import TokenUsage


class ModelError(Exception):
    """所有 MatterLoop 模型异常的基类。"""


class ModelAlreadyRegisteredError(ModelError):
    """同名模型已存在且调用方没有允许替换。"""


class ModelNotFoundError(ModelError):
    """注册表中不存在请求的模型。"""


class FakeModelExhaustedError(ModelError):
    """假模型没有剩余响应可供测试消费。"""


class ModelInvocationError(ModelError):
    """模型供应商调用失败，且错误文本已避免暴露敏感信息。"""


class ModelAuthenticationError(ModelInvocationError):
    """模型供应商拒绝了客户端身份，且异常不包含凭据内容。"""


class ModelPaymentRequiredError(ModelInvocationError):
    """模型供应商因余额或付费状态拒绝了调用。"""


class ModelRateLimitError(ModelInvocationError):
    """模型供应商拒绝了超过速率或并发限制的调用。"""


class ModelServiceError(ModelInvocationError):
    """模型供应商服务端暂时无法完成调用。"""


class ModelResponseParseError(ModelError):
    """供应商返回了无法归一化的响应结构。

    Args:
        message: 不包含供应商原始响应或凭据的安全错误说明。
        usage: 供应商已经完成计费时可提取的实际 Token 用量。

    Notes:
        解析失败不代表远端调用没有产生费用。预算包装器可利用 ``usage`` 完成结算后再
        把错误交给上层处理。
    """

    def __init__(self, message: str, *, usage: TokenUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class ModelCapabilityError(ModelError):
    """当前供应商、模型或模式不支持请求中的能力组合。"""
