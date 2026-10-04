"""输入校验助手，服务层与 API 层共用。"""
from __future__ import annotations

from .errors import ValidationError


def require_text(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"字段 {field} 必须是非空字符串")
    return value.strip()


def optional_text(field: str, value: object) -> str | None:
    if value is None:
        return None
    return require_text(field, value)


def require_int(field: str, value: object, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"字段 {field} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValidationError(f"字段 {field} 不能小于 {minimum}")
    return value


def optional_int(field: str, value: object, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return require_int(field, value, minimum=minimum)
