"""
extractors/cicd_extractor.py
-----------------------------------------------------------------------------
Phase 2.3 — single extractor for the three CI/CD sources.

The collector envelopes emitted by ``GitHubActionsCollector``,
``GitLabCICollector``, and ``JenkinsCollector`` share the same shape
(envelope ``type`` plus ``runs`` list of ``PipelineRun`` dicts). One
extractor covers all three; per-provider source names are preserved
via the ``source`` field on the observation.

One ``Observation`` per incident-relevant run:

- one observation per failed pipeline run
- one observation per running / pending pipeline run in window
- one observation per pipeline run that explicitly references a
  deployment environment (deployment linkage)
- one per failed stage (deduped by stage name)

All observations share ``kind="generic_log_line"``; ``source`` is the
provider identifier so provenance is preserved.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List

from extractors.base import (
    BaseExtractor,
    ExtractionContext,
    Location,
    Observation,
)
from extractors.registry import ExtractorMetadata, registry


# Map envelope type → provider source key.
_ENVELOPE_TO_SOURCE = {
    "github_actions_runs": "github_actions",
    "gitlab_ci_pipelines": "gitlab_ci",
    "jenkins_builds": "jenkins",
}


@registry.register(
    ExtractorMetadata(
        extractor_id="github_actions_extractor",
        version="1.0.0",
        source="github_actions",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.3 — parses the JSON envelope emitted by "
            "GitHubActionsCollector into Observations."
        ),
    )
)
@registry.register(
    ExtractorMetadata(
        extractor_id="gitlab_ci_extractor",
        version="1.0.0",
        source="gitlab_ci",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.3 — parses the JSON envelope emitted by "
            "GitLabCICollector into Observations."
        ),
    )
)
@registry.register(
    ExtractorMetadata(
        extractor_id="jenkins_extractor",
        version="1.0.0",
        source="jenkins",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.3 — parses the JSON envelope emitted by "
            "JenkinsCollector into Observations."
        ),
    )
)
class CICDExtractor(BaseExtractor):
    """Parses any of the three CI/CD envelope types."""

    EXTRACTOR_ID = "cicd_extractor"

    def extract(self, context: ExtractionContext) -> List[Observation]:
        observations: List[Observation] = []
        raw = context.raw_content
        if not raw or not raw.strip():
            return observations

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return observations
        if not isinstance(payload, dict):
            return observations

        envelope_type = payload.get("type")
        if not isinstance(envelope_type, str):
            return observations
        source = _ENVELOPE_TO_SOURCE.get(envelope_type)
        if source is None:
            return observations

        service_hint = payload.get("service")
        if not isinstance(service_hint, str):
            service_hint = None
        runs = payload.get("runs") or []
        if not isinstance(runs, list):
            return observations

        seen_run_keys: set = set()
        seen_stage_keys: set = set()

        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = str(run.get("id") or "")
            status = (
                str(run.get("status") or "")
                if isinstance(run.get("status"), str)
                else None
            )
            conclusion = (
                str(run.get("conclusion") or "")
                if isinstance(run.get("conclusion"), str)
                else None
            )
            env = (
                str(run.get("environment") or "")
                if isinstance(run.get("environment"), str)
                else None
            )
            branch = (
                str(run.get("branch") or "")
                if isinstance(run.get("branch"), str)
                else None
            )
            commit_sha = (
                str(run.get("commit_sha") or "")
                if isinstance(run.get("commit_sha"), str)
                else None
            )
            name = (
                str(run.get("name") or "")
                if isinstance(run.get("name"), str)
                else None
            )
            actor = (
                str(run.get("actor") or "")
                if isinstance(run.get("actor"), str)
                else None
            )
            url = (
                str(run.get("url") or "")
                if isinstance(run.get("url"), str)
                else None
            )
            failure_message = (
                str(run.get("failure_message") or "")
                if isinstance(run.get("failure_message"), str)
                else None
            )

            run_key = f"{source}:run:{run_id}"
            if run_key in seen_run_keys:
                continue

            is_failed = _is_failed(conclusion) or _is_failed(status)
            is_running = status in ("running", "in_progress", "queued")
            is_deployment = bool(env and env != "run")

            if is_failed or is_running or is_deployment:
                seen_run_keys.add(run_key)
                observations.append(
                    self._build_run_observation(
                        context=context,
                        source=source,
                        run_id=run_id,
                        name=name,
                        status=status,
                        conclusion=conclusion,
                        branch=branch,
                        commit_sha=commit_sha,
                        environment=env,
                        url=url,
                        actor=actor,
                        failure_message=failure_message,
                        started_at=run.get("started_at"),
                        finished_at=run.get("finished_at"),
                        is_deployment=is_deployment,
                        service_hint=service_hint,
                    )
                )

            stages = run.get("stages") or []
            if not isinstance(stages, list):
                continue
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                stage_name = (
                    str(stage.get("name") or "")
                    if isinstance(stage.get("name"), str)
                    else None
                )
                if not stage_name:
                    continue
                stage_status = (
                    str(stage.get("status") or "")
                    if isinstance(stage.get("status"), str)
                    else None
                )
                stage_conclusion = (
                    str(stage.get("conclusion") or "")
                    if isinstance(stage.get("conclusion"), str)
                    else None
                )
                stage_key = f"{source}:stage:{run_id}:{stage_name}"
                if stage_key in seen_stage_keys:
                    continue
                if not _is_failed(stage_conclusion) and not _is_failed(
                    stage_status
                ):
                    continue
                seen_stage_keys.add(stage_key)
                observations.append(
                    self._build_stage_observation(
                        context=context,
                        source=source,
                        run_id=run_id,
                        stage=stage,
                        stage_name=stage_name,
                        stage_status=stage_status,
                        stage_conclusion=stage_conclusion,
                        branch=branch,
                        commit_sha=commit_sha,
                        service_hint=service_hint,
                    )
                )

        return observations

    # ------------------------------------------------------------------
    # Observation builders
    # ------------------------------------------------------------------
    def _build_run_observation(
        self,
        *,
        context: ExtractionContext,
        source: str,
        run_id: str,
        name: Optional[str],
        status: Optional[str],
        conclusion: Optional[str],
        branch: Optional[str],
        commit_sha: Optional[str],
        environment: Optional[str],
        url: Optional[str],
        actor: Optional[str],
        failure_message: Optional[str],
        started_at: Any,
        finished_at: Any,
        is_deployment: bool,
        service_hint: Optional[str],
    ) -> Observation:
        data: Dict[str, Any] = {
            "kind": "cicd_run",
            "run_id": run_id,
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "branch": branch,
            "commit_sha": commit_sha,
            "environment": environment,
            "actor": actor,
            "is_deployment": is_deployment,
        }
        if failure_message:
            data["failure_message"] = failure_message[:256]
        extracted_at = _first_iso(started_at, finished_at) or _iso_now()
        location = Location(
            step_name=f"{source}:run:{run_id}",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(
                {
                    "run_id": run_id,
                    "status": status,
                    "conclusion": conclusion,
                },
                sort_keys=True,
            ),
            extracted_at=extracted_at,
            service=service_hint,
            resource=f"{source}:run/{run_id}",
            normalized_value=conclusion or status or "unknown",
            timestamp_known=bool(started_at or finished_at),
        )

    def _build_stage_observation(
        self,
        *,
        context: ExtractionContext,
        source: str,
        run_id: str,
        stage: Dict[str, Any],
        stage_name: str,
        stage_status: Optional[str],
        stage_conclusion: Optional[str],
        branch: Optional[str],
        commit_sha: Optional[str],
        service_hint: Optional[str],
    ) -> Observation:
        data = {
            "kind": "cicd_stage",
            "run_id": run_id,
            "stage": stage_name,
            "status": stage_status,
            "conclusion": stage_conclusion,
            "branch": branch,
            "commit_sha": commit_sha,
        }
        failure = stage.get("failure_message")
        if isinstance(failure, str) and failure:
            data["failure_message"] = failure[:256]
        extracted_at = _first_iso(
            stage.get("started_at"), stage.get("finished_at")
        ) or _iso_now()
        location = Location(
            step_name=f"{source}:stage:{run_id}/{stage_name}",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(
                {"run_id": run_id, "stage": stage_name},
                sort_keys=True,
            ),
            extracted_at=extracted_at,
            service=service_hint,
            resource=f"{source}:run/{run_id}/stage/{stage_name}",
            normalized_value=stage_conclusion or stage_status or "failed",
            timestamp_known=bool(
                stage.get("started_at") or stage.get("finished_at")
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_failed(value: Optional[str]) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.strip().lower()
    return lowered in (
        "failure",
        "failed",
        "cancelled",
        "canceled",
        "timed_out",
        "timeout",
        "unstable",
        "error",
    )


def _first_iso(*values: Any) -> Optional[str]:
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        text = v.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    return None


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["CICDExtractor"]
