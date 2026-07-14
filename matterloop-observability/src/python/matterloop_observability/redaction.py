"""可观测性输出的敏感字段脱敏。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class Redactor:
    """递归过滤默认和用户声明的敏感字段。"""

    def __init__(self, extra_fields: tuple[str, ...] = ()) -> None:
        defaults = ("token", "authorization", "cookie", "api_key", "password", "secret")
        self._fields = {_normalize_field(field) for field in (*defaults, *extra_fields)}

    def redact(self, value: object) -> object:
        """返回不修改原对象的脱敏副本。"""
        if isinstance(value, Mapping):
            return {
                str(key): "[REDACTED]" if self._is_sensitive(str(key)) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact(item) for item in value]
        return value

    def _is_sensitive(self, key: str) -> bool:
        """识别常见前后缀形式，例如 ``access_token`` 和 ``set-cookie``。"""
        normalized = _normalize_field(key)
        return any(
            normalized == field
            or normalized.startswith(f"{field}_")
            or normalized.endswith(f"_{field}")
            for field in self._fields
        )


def _normalize_field(field: str) -> str:
    """统一不同日志系统常见的字段分隔符。"""
    return field.casefold().replace("-", "_").replace(".", "_")
