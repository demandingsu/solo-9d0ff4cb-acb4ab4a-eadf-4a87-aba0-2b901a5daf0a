"""Domain exceptions mapped to HTTP status codes in the API layer."""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400


class NotFoundError(DomainError):
    status_code = 404


class StaleVersionError(DomainError):
    """Optimistic-concurrency failure: the client's snapshot is outdated."""

    status_code = 409

    def __init__(self, submitted: int, current: int, message: str | None = None):
        self.submitted = submitted
        self.current = current
        super().__init__(
            message
            or f"提交基于过期版本 {submitted}，当前版本为 {current}，请重新获取快照后再提交"
        )


class ConflictError(DomainError):
    status_code = 409
