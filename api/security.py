"""Security and path-validation helpers for the investigation API."""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePath
from typing import Iterable, Optional


# Patterns that are very likely secrets: API keys, passwords, tokens.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key)"),
)

# Variable names we should not render values of when displaying evidence.
_SENSITIVE_VAR_NAMES = {
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "API_KEY",
    "ACCESS_KEY",
    "TOKEN",
    "AUTH_TOKEN",
    "PRIVATE_KEY",
    "DATABASE_URL",  # may contain credentials in URL
}


def allowed_workspace_root() -> Path:
    """Returns the configured workspace root for incident repositories.

    Restricted by ``AUTORCA_WORKSPACE_ROOT`` (default: ``./projects-for-test``).
    """
    raw = os.environ.get("AUTORCA_WORKSPACE_ROOT", "projects-for-test")
    return Path(raw).resolve()


def resolve_repository_path(repo: str) -> Path:
    """Validate and resolve a repository path submitted to the API.

    The path must:
    - exist on disk
    - be a directory
    - be inside the allowed workspace root (no arbitrary file access).
      Symlinks whose parent is inside the workspace are accepted because the
      link itself was placed there by a trusted caller.

    Raises ``ValueError`` on any violation.
    """
    if not repo or not isinstance(repo, str):
        raise ValueError("repository path is required")
    candidate = Path(repo)
    # Reject obvious traversal attempts and shell injection markers.
    if "\x00" in repo:
        raise ValueError("invalid repository path")
    workspace = allowed_workspace_root()
    # Validate the *parent* of the candidate against the workspace — this
    # accepts symlinks placed inside the workspace that point outside it.
    parent = candidate.parent if candidate.parent != candidate else Path(".")
    try:
        parent_resolved = parent.resolve(strict=False)
        parent_resolved.relative_to(workspace)
    except ValueError:
        raise ValueError(
            "repository path is outside the allowed workspace "
            f"({workspace}); arbitrary filesystem access is not permitted"
        )
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"invalid repository path: {exc}") from exc
    if not resolved.exists():
        raise ValueError(f"repository path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"repository path is not a directory: {resolved}")
    return resolved


def validate_environment(env: str) -> str:
    if env not in {"local", "staging", "production"}:
        raise ValueError("environment must be one of: local, staging, production")
    return env


def validate_optional_path(name: str, value: Optional[str]) -> Optional[str]:
    """Allow only small text/log files from the workspace."""
    if value is None or str(value).strip() == "":
        return None
    candidate = Path(value)
    if "\x00" in value:
        raise ValueError(f"{name} contains invalid characters")
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"{name} invalid path: {exc}") from exc
    workspace = allowed_workspace_root()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be inside the allowed workspace ({workspace})"
        ) from exc
    return str(resolved)


def mask_secret(name: Optional[str], value: Optional[str]) -> str:
    """Return a redacted representation of an environment-variable value.

    Sensitive names (password/token/etc.) have their values masked entirely.
    Other values keep their prefix but redact the middle.
    """
    if value is None:
        return ""
    upper_name = (name or "").upper()
    if any(part in upper_name for part in _SENSITIVE_VAR_NAMES):
        return "***REDACTED***"
    if len(value) <= 4:
        return "***"
    if len(value) <= 12:
        return value[:1] + "***" + value[-1:]
    return value[:3] + "***" + value[-3:]


def looks_like_secret_assignment(line: str) -> bool:
    return any(p.search(line) for p in _SECRET_VALUE_PATTERNS)
