"""定义 Celery 集成可由调用方分类处理的异常。"""


class CeleryIntegrationError(Exception):
    """所有 MatterLoop Celery 集成异常的基类。"""


class CeleryPayloadError(CeleryIntegrationError):
    """Celery 消息不符合版本化 DTO Schema。"""


class CeleryFactoryError(CeleryIntegrationError):
    """Worker 无法解析或调用 Runtime 工厂。"""


class CeleryRunConflictError(CeleryIntegrationError):
    """消息中的运行请求与共享仓储记录不一致。"""


class CeleryWorkerError(CeleryIntegrationError):
    """Worker Runtime 返回了无法持久化的结果。"""
