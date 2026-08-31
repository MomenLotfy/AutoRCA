"""Domain‑level exceptions used across the persistence layer.

These exceptions are purposefully lightweight and do not import any heavy
application modules to avoid circular import problems.
"""

class PersistenceUnavailableError(RuntimeError):
    """Raised when the PostgreSQL backend cannot be reached or is otherwise unavailable."""

class ForbiddenAccessError(RuntimeError):
    """Raised when an investigation is accessed outside of the authorized workspace/tenant."""

class InvalidInvestigationIdError(RuntimeError):
    """Raised when an investigation identifier does not match the required pattern."""
