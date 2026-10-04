"""领域异常与统一错误码。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类，携带 HTTP 状态码与机器可读错误码。"""

    code = "domain_error"
    http_status = 409

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class StateError(DomainError):
    """状态机不允许的迁移。"""

    code = "state_conflict"
    http_status = 409


class DuplicateError(DomainError):
    code = "duplicate"
    http_status = 409


class CapacityError(DomainError):
    code = "capacity_exceeded"
    http_status = 409


class InsufficientStockError(DomainError):
    code = "insufficient_stock"
    http_status = 409
