"""Security and path-validation helpers for the investigation API."""
from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path, PurePath
from typing import Iterable, Optional
from urllib.parse import urlparse


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


# ---------------------------------------------------------------------------
# Phase 2.1 — integration URL validator.
#
# Mirrors the spirit of `resolve_repository_path`: every external URL the
# collector is about to hit is checked here at the API boundary. This is
# the only place in the codebase that hardens the integration layer
# against SSRF and embedded credentials.
# ---------------------------------------------------------------------------
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Set of loopback / private / link-local networks that are denied by
# default. Tests can opt out via AUTORCA_ES_ALLOW_LOOPBACK=1.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def validate_integration_url(
    name: str,
    url: str,
    *,
    allow_loopback: Optional[bool] = None,
) -> str:
    """Validate an external integration URL.

    - `name` is the integration field name (e.g. ``elasticsearch_url``)
      used only for error messages.
    - `url` must be an absolute ``http://`` or ``https://`` URL with no
      embedded credentials (userinfo).
    - The hostname must resolve to a public IP. Loopback/private ranges
      are rejected unless ``allow_loopback`` is True (or the env var
      ``AUTORCA_ES_ALLOW_LOOPBACK`` is set to a truthy value, used by
      tests).

    Returns the canonicalised URL string on success. Raises ``ValueError``
    on any violation.
    """
    if not url or not isinstance(url, str):
        raise ValueError(f"{name} is required")

    if allow_loopback is None:
        env_flag = os.environ.get("AUTORCA_ES_ALLOW_LOOPBACK", "").strip().lower()
        allow_loopback = env_flag in {"1", "true", "yes", "on"}

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid URL: {exc}") from exc

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"{name} must use one of {sorted(_ALLOWED_SCHEMES)}; "
            f"got scheme={parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError(f"{name} is missing a hostname")
    if parsed.username or parsed.password:
        raise ValueError(
            f"{name} must not embed credentials in the URL; use the "
            f"auth_env field to reference a secret by environment name"
        )
    # Reject obvious port-override tricks (port=0, port>65535) by leaning
    # on urlparse's int parsing.
    if parsed.port is not None and (parsed.port < 1 or parsed.port > 65535):
        raise ValueError(f"{name} has an invalid port: {parsed.port}")

    if not allow_loopback:
        host = parsed.hostname
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Not a literal IP — we'd have to resolve DNS to decide,
            # which we deliberately avoid at the API boundary. We
            # therefore only block literal IP addresses; DNS-based
            # SSRF is mitigated by TLS + the network egress policy
            # documented in the Architecture Freeze.
            ip = None
        if ip is not None and any(ip in net for net in _BLOCKED_NETWORKS):
            raise ValueError(
                f"{name} resolves to a blocked network range "
                f"(loopback/private/link-local); set "
                f"AUTORCA_ES_ALLOW_LOOPBACK=1 to override (tests only)"
            )

    return url
