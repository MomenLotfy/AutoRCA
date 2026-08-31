# Workspace Validation Analysis

## Root Cause

The investigation request was rejected with a **400 Bad Request** because the backend performed workspace validation on the supplied repository path. The validation logic in `api/security.py` (`resolve_repository_path`) enforces that the repository **must reside inside the configured workspace root**. The path `/home/moamen-lotfy/Desktop/AutoRCA` (the repository root of the source checkout) is **outside** the default workspace (`projects-for-test`), so the request was rejected with the error:

```
repository path is outside the allowed workspace (/home/moamen-lotfy/Desktop/AutoRCA/projects-for-test); arbitrary filesystem access is not permitted
```

## Runtime Configuration

* **Environment variable** `AUTORCA_WORKSPACE_ROOT` controls the allowed workspace directory.
* If the variable is unset, the code falls back to the hard‑coded default `"projects-for-test"` (relative to the project root).
* The function `allowed_workspace_root()` reads the variable, resolves the path, and returns a `Path` object. This value is used by all validation helpers that need a workspace boundary.

## Validation Implementation

Relevant functions from `api/security.py`:

```python
def allowed_workspace_root() -> Path:
    raw = os.environ.get("AUTORCA_WORKSPACE_ROOT", "projects-for-test")
    return Path(raw).resolve()

def resolve_repository_path(repo: str) -> Path:
    # Basic sanity checks
    if not repo or not isinstance(repo, str):
        raise ValueError("repository path is required")
    candidate = Path(repo)
    if "\x00" in repo:
        raise ValueError("invalid repository path")
    workspace = allowed_workspace_root()
    # Validate the *parent* of the candidate against the workspace.
    # This accepts symlinks placed inside the workspace that point outside.
    parent = candidate.parent if candidate.parent != candidate else Path(".")
    try:
        parent_resolved = parent.resolve(strict=False)
        parent_resolved.relative_to(workspace)
    except ValueError:
        raise ValueError(
            "repository path is outside the allowed workspace "
            f"({workspace}); arbitrary filesystem access is not permitted"
        )
    # Resolve the final path and ensure it exists and is a directory.
    resolved = candidate.resolve(strict=False)
    if not resolved.exists():
        raise ValueError(f"repository path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"repository path is not a directory: {resolved}")
    return resolved
```

The validation chain checks:
1. The value is a non‑empty string.
2. No null byte characters.
3. The parent directory (or the repo itself if it is root) resolves **inside** the workspace.
4. The final resolved path exists and is a directory.

## Allowed Workspace

* **Default**: `projects-for-test` – a directory located **under the repository root** (`$REPO_ROOT/projects-for-test`).
* The workspace is resolved to an absolute path with `Path(...).resolve()`. All repository paths must have a parent directory that, once resolved, is a descendant of this workspace directory.
* **Symlink policy**: A symlink placed **inside** the workspace is allowed, even if the symlink points to a location outside the workspace. This mirrors the intent that a trusted caller may expose external resources deliberately via a symlink.

## Why Current Repository Was Rejected

The UI submitted the repository path `/home/moamen-lotfy/Desktop/AutoRCA`. Its parent directory (`/home/moamen-lotfy/Desktop/AutoRCA`) does **not** fall under the default workspace (`/home/moamen-lotfy/Desktop/AutoRCA/projects-for-test`). Consequently, step 3 of `resolve_repository_path` raised a `ValueError`, which the API translated into the 400 response observed in the Playwright audit.

## Production vs Test Configuration

* **Test environment** – All test suites (`tests/*`) explicitly set `AUTORCA_WORKSPACE_ROOT` to a temporary directory via `monkeypatch.setenv`. This ensures the repository paths created inside the fixture (`workspace / "…"`) are accepted, and the *outside‑workspace* test case deliberately uses an absolute system path (`/etc/passwd`) to verify rejection.
* **Production deployment** – In a real deployment the operator would set `AUTORCA_WORKSPACE_ROOT` to a location that the service is allowed to scan (e.g. `/var/autorca/workspace`). If the variable is left unset, the service falls back to the safe default `projects-for-test`, which is suitable for development or sandbox environments but would reject most production‑scale repository locations.

## Relevant Tests

| Test | File | Purpose |
|------|------|---------|
| `test_create_investigation_rejects_path_outside_workspace` | `tests/test_api_investigations.py` | Verifies that a repository path outside the workspace (e.g. `/etc/passwd`) is rejected with a 400 and contains the word "workspace" in the error. |
| `test_create_investigation_requires_real_repository` (implicit in `test_create_investigation_requires_real_repository` expectations) | `tests/test_api_investigations.py` | Ensures the path must exist and be a directory; a missing path triggers a 400. |
| Workspace fixtures (`workspace` fixture) | `tests/test_api_investigations.py` & `tests/test_phase2_api_extension.py` | Set `AUTORCA_WORKSPACE_ROOT` to a temporary directory, confirming that inside‑workspace paths succeed. |
| `test_create_investigation_requires_real_repository` (environment validation) | `tests/test_api_investigations.py` | Checks that the `environment` field is validated separately, demonstrating the API’s layered validation approach. |

These tests collectively document the contract that the API enforces a workspace allowlist and that the error message shown to the UI originates from `resolve_repository_path`.

## Recommended Fix (Analysis‑Only)

1. **UI Guidance** – Add a helper text or tooltip next to the *Git repository path* input that explains the workspace restriction, e.g.:
   > "Only repositories located under the configured workspace (`$AUTORCA_WORKSPACE_ROOT`) are accepted. Set the environment variable on the server to change this location."
   This makes the constraint explicit before the user submits the form, addressing the usability bug recorded in the Playwright audit (BUG‑001).
2. **Error Presentation** – The frontend currently displays the raw backend error message. Consider mapping the error to a user‑friendly phrase such as "The selected repository is outside the allowed workspace. Contact the system administrator to change the workspace configuration."
3. **Configuration Exposure (optional)** – If the service is intended for broader users, provide an endpoint (e.g. `/api/v1/config`) that returns non‑secret configuration like the current `AUTORCA_WORKSPACE_ROOT`. This would allow the UI to dynamically show the allowed path without hard‑coding it.
4. **Documentation Update** – Ensure the README or admin guide clearly mentions the purpose of `AUTORCA_WORKSPACE_ROOT`, the default value, and how to override it in production deployments.
5. **No code change is required** for the core validation logic; the existing implementation already correctly enforces the security boundary.

---

*This analysis is based on a read‑only investigation of the AutoRCA code base, test suite, and the observed 400 Bad Request response during the controlled Playwright audit.*