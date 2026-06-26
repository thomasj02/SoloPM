"""Domain errors.

Every domain failure carries a stable ``code`` so the HTTP layer can map it to a
status and the CLI can branch on it reliably (the ``--json`` failure contract).
"""

from __future__ import annotations


class SoloPMError(Exception):
    """Base class for all domain errors.

    Attributes:
        code: stable, machine-readable error code (see ``API.md``).
        message: human-readable message.
        status: suggested HTTP status code for the server layer.
    """

    code: str = "error"
    status: int = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFoundError(SoloPMError):
    code = "not_found"
    status = 404


class ValidationError(SoloPMError):
    code = "validation"
    status = 400


class DuplicateError(SoloPMError):
    code = "duplicate"
    status = 409


class InvalidTransitionError(SoloPMError):
    code = "invalid_transition"
    status = 409


class ForbiddenTransitionError(SoloPMError):
    code = "forbidden_transition"
    status = 403
