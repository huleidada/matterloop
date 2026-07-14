"""Redis 集成异常。"""


class RedisIntegrationError(Exception):
    """所有 Redis 集成异常的基类。"""


class RedisPayloadError(RedisIntegrationError):
    """Redis 中的运行数据不符合当前 Schema。"""
