from __future__ import annotations

import json
import mimetypes
import sys
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from collectors.file_collector import FileCollector, FileCollectorError
from collectors.git_collector import GitCollector, GitCollectorError
from llm import LLMAnalysisService, LLMClientError, OpenAICompatibleLLMClient
from pipeline import AnalysisPipeline, PipelineInput
from rca_request.rca_request_builder import RCARequestBuilder, RepositoryContext

from api.investigation_service import (
    AnalysisRequestError,
    InvestigationService,
    investigation_to_response,
)
from persistence.exceptions import PersistenceUnavailableError, ForbiddenAccessError, InvalidInvestigationIdError

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "web" / "static"
RULES_CONFIG = PROJECT_ROOT / "rules" / "rules.config.json"
TAXONOMY = PROJECT_ROOT / "taxonomy" / "taxonomy.yaml"


class AnalysisRequestError(ValueError):
    pass


def _analysis_id() -> str:
    return f"AR{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%f')[:-3]}"


# ---------------------------------------------------------------------------
# Legacy /api/analyze (kept for backward compatibility with existing tests).
# ---------------------------------------------------------------------------
def analyze_incident(payload: dict) -> dict:
    repo = str(payload.get("repo", "")).strip()
    environment = str(payload.get("environment", "")).strip()
    if not repo:
        raise AnalysisRequestError("A real Git repository path is required.")
    if environment not in {"local", "staging", "production"}:
        raise AnalysisRequestError("Environment must be local, staging, or production.")

    try:
        git = GitCollector(repo)
    except GitCollectorError as exc:
        raise AnalysisRequestError(str(exc)) from exc

    sources: dict = {}
    if not payload.get("no_diff", False):
        try:
            sources["git_diff"] = git.collect_diff(commit=payload.get("commit") or None)
        except GitCollectorError:
            pass

    source_fields = (
        ("traceback", "traceback"),
        ("docker_log", "docker_output"),
        ("ci_log", "ci_log"),
    )
    for field, source_name in source_fields:
        path = str(payload.get(field, "")).strip()
        if path:
            try:
                sources[source_name] = FileCollector.collect(path)
            except FileCollectorError as exc:
                raise AnalysisRequestError(str(exc)) from exc

    if not sources:
        raise AnalysisRequestError(
            "No incident data collected. Provide a real traceback/log path or allow Git diff collection."
        )

    try:
        commit_sha = payload.get("commit") or git.resolve_commit_sha()
    except GitCollectorError:
        commit_sha = "unknown"
    try:
        branch = git.resolve_branch()
    except GitCollectorError:
        branch = "unknown"

    pipeline = AnalysisPipeline.from_config_files(RULES_CONFIG, TAXONOMY)
    result = pipeline.run(PipelineInput(analysis_id=_analysis_id(), sources=sources))
    selected = result.selected
    deterministic = {
        "analysis_id": result.analysis_id,
        "environment": environment,
        "commit_sha": commit_sha,
        "branch": branch,
        "repository": str(payload.get("full_name", "")).strip() or Path(repo).name,
        "root_cause": selected.label if selected else None,
        "confidence": pipeline.compute_confidence(selected.score) if selected else None,
        "severity": (
            pipeline.resolve_severity(
                selected.failure_type_id,
                {"environment": environment},
            )
            if selected
            else None
        ),
        "evidence": result.evidence_list,
    }

    if not selected:
        return {"deterministic": deterministic, "final_rca": None}

    builder = RCARequestBuilder(
        rule_engine=pipeline._rule_engine,
        scoring_engine=pipeline._scoring_engine,
        taxonomy_index=pipeline._taxonomy_index,
        rules_config=pipeline._rules_config,
    )
    request = builder.build(
        analysis_id=result.analysis_id,
        selected=selected,
        all_hypotheses=result.hypotheses,
        evidence_list=result.evidence_list,
        repository_context=RepositoryContext(
            full_name=deterministic["repository"],
            branch=branch,
            commit_sha=commit_sha,
            environment=environment,
        ),
        diff_source=sources.get("git_diff"),
        log_source=(
            sources.get("traceback")
            or sources.get("ci_log")
            or sources.get("docker_output")
        ),
    )
    try:
        final_rca = LLMAnalysisService(
            OpenAICompatibleLLMClient()
        ).generate_validated(request)
    except LLMClientError as exc:
        raise AnalysisRequestError(str(exc)) from exc

    return {"deterministic": deterministic, "final_rca": final_rca}


# ---------------------------------------------------------------------------
# Investigation v1 service — a single process-wide instance.
# ---------------------------------------------------------------------------
_INVESTIGATION_LOCK = threading.Lock()
_INVESTIGATION_SERVICE: InvestigationService | None = None
_INVESTIGATION_PIPELINE: AnalysisPipeline | None = None


def _get_investigation_service() -> InvestigationService:
    global _INVESTIGATION_SERVICE, _INVESTIGATION_PIPELINE
    with _INVESTIGATION_LOCK:
        if _INVESTIGATION_SERVICE is None:
            _INVESTIGATION_PIPELINE = AnalysisPipeline.from_config_files(
                RULES_CONFIG, TAXONOMY
            )
            _INVESTIGATION_SERVICE = InvestigationService(_INVESTIGATION_PIPELINE)
    return _INVESTIGATION_SERVICE


def reset_investigation_service() -> None:
    """Test helper — drop the cached service so a fresh one is created."""
    global _INVESTIGATION_SERVICE, _INVESTIGATION_PIPELINE
    with _INVESTIGATION_LOCK:
        _INVESTIGATION_SERVICE = None
        _INVESTIGATION_PIPELINE = None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class AutoRCAHandler(BaseHTTPRequestHandler):
    server_version = "AutoRCAWeb/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # Static UI
        if path == "/":
            self._serve_static("index.html")
            return
        if path in {"/app.js", "/styles.css"}:
            self._serve_static(path.lstrip("/"))
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # API v1 routes
        if path == "/api/health":
            self._send_json({"status": "ok", "service": "autorca-web"}, 200)
            return

        if path == "/api/v1/investigations":
            self._list_investigations()
            return

        # /api/v1/investigations/{id}
        investigation_match = self._match_investigation(path)
        if investigation_match is not None:
            investigation_id, sub = investigation_match
            if sub in ("", None):
                self._get_investigation(investigation_id)
                return
            if sub == "evidence":
                self._get_investigation_section(investigation_id, "evidence")
                return
            if sub == "observations":
                self._get_investigation_section(investigation_id, "observations")
                return
            if sub == "timeline":
                self._get_investigation_section(investigation_id, "timeline")
                return
            if sub == "graph":
                self._get_investigation_section(investigation_id, "graph")
                return
            if sub == "correlation":
                self._get_investigation_section(investigation_id, "correlation")
                return
            if sub == "remediation":
                self._get_investigation_section(investigation_id, "remediation")
                return
            if sub == "fingerprint":
                self._get_investigation_section(investigation_id, "fingerprint")
                return
            if sub == "hypothesis":
                self._get_investigation_section(investigation_id, "hypothesis_assessment")
                return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # Legacy deterministic endpoint (kept for backwards compatibility)
        if path == "/api/analyze":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 64 * 1024:
                    raise AnalysisRequestError("Request is too large.")
                payload = json.loads(self.rfile.read(length))
                result = analyze_incident(payload)
                self._send_json(result, 200)
            except (json.JSONDecodeError, AnalysisRequestError) as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": "internal server error"}, 500)
            return

        if path == "/api/v1/investigations":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 256 * 1024:
                    raise AnalysisRequestError("Request is too large.")
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw or b"{}")
                service = _get_investigation_service()
                investigation = service.create_investigation(payload)
                self._send_json(investigation.payload, 201)
            except json.JSONDecodeError as exc:
                self._send_json({"error": f"invalid JSON: {exc}"}, 400)
            except AnalysisRequestError as exc:
                self._send_json({"error": str(exc)}, 400)
            except PersistenceUnavailableError as exc:
                self._send_json({"error": "service unavailable"}, 503)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": "internal server error"}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    # ------------------------------------------------------------------
    # Investigation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _match_investigation(path: str) -> tuple[str, str] | None:
        prefix = "/api/v1/investigations/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        if not rest:
            return None
        parts = rest.split("/", 1)
        investigation_id = parts[0]
        sub = parts[1] if len(parts) > 1 else ""
        if not investigation_id:
            return None
        return investigation_id, sub

    def _list_investigations(self) -> None:
        service = _get_investigation_service()
        try:
            items = [investigation_to_response(inv) for inv in service.list_investigations()]
            self._send_json({"investigations": items, "count": len(items)}, 200)
        except PersistenceUnavailableError as exc:
            self._send_json({"error": "service unavailable"}, 503)
        except Exception as exc:
            self._send_json({"error": "internal server error"}, 500)

    def _get_investigation(self, investigation_id: str) -> None:
        service = _get_investigation_service()
        try:
            investigation = service.get_investigation(investigation_id)
        except ForbiddenAccessError as exc:
            self._send_json({"error": "access denied"}, 404)
            return
        except PersistenceUnavailableError as exc:
            self._send_json({"error": "service unavailable"}, 503)
            return
        except InvalidInvestigationIdError as exc:
            self._send_json({"error": "investigation not found"}, 404)
            return
        except Exception as exc:
            self._send_json({"error": "internal server error"}, 500)
            return
        if investigation is None:
            self._send_json({"error": "investigation not found"}, 404)
            return
        self._send_json(investigation.payload, 200)

    def _get_investigation_section(self, investigation_id: str, section: str) -> None:
        service = _get_investigation_service()
        try:
            investigation = service.get_investigation(investigation_id)
        except ForbiddenAccessError as exc:
            self._send_json({"error": "access denied"}, 404)
            return
        except PersistenceUnavailableError as exc:
            self._send_json({"error": "service unavailable"}, 503)
            return
        except InvalidInvestigationIdError as exc:
            self._send_json({"error": "investigation not found"}, 404)
            return
        except Exception as exc:
            self._send_json({"error": "internal server error"}, 500)
            return
        if investigation is None:
            self._send_json({"error": "investigation not found"}, 404)
            return
        section_data = investigation.payload.get(section)
        if section_data is None:
            self._send_json(
                {"investigation_id": investigation_id, "section": section, "data": None, "available": False},
                200,
            )
            return
        self._send_json(
            {"investigation_id": investigation_id, "section": section, "data": section_data, "available": True},
            200,
        )

    # ------------------------------------------------------------------
    # Static + JSON plumbing
    # ------------------------------------------------------------------
    def _serve_static(self, name: str) -> None:
        path = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT not in path.parents or not path.is_file():
            self._send_json({"error": "Not found"}, 404)
            return
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "text/plain")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, value: dict, status: int) -> None:
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} - {format % args}", file=sys.stderr)


def main() -> None:
    port = int(__import__("os").environ.get("PORT", "5000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AutoRCAHandler)
    print(f"AutoRCA UI listening on http://0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
