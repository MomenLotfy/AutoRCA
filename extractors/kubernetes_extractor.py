"""
extractors/kubernetes_extractor.py
-----------------------------------------------------------------------------
Phase 2.3 — parse the JSON envelope emitted by ``KubernetesCollector``
(``source="kubernetes"``) into ``Observation`` objects.

The envelope carries:

    {
      "type": "kubernetes_state",
      "namespace": "<ns>",
      "pod_count": N, "event_count": M, "deployment_count": K,
      "incident_reasons": ["OOMKilled", "CrashLoopBackOff", ...],
      "pods": [...], "events": [...], "deployments": [...]
    }

One ``Observation`` per incident-relevant finding:

- one observation per pod that has at least one incident reason
  (OOMKilled / CrashLoopBackOff / ErrImagePull / …)
- one observation per Warning event with an incident reason
- one observation per deployment with unavailable replicas
- one observation per pod whose restart count > 0 (severity scales
  with count)
- one observation per namespace summary if no incident findings exist
  (low-severity "namespace queried but clean" record)

All observations share ``kind="generic_log_line"`` so the schema
needs no bump. ``source="kubernetes"`` keeps provenance intact.
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


_INCIDENT_REASONS = {
    "OOMKilled",
    "CrashLoopBackOff",
    "ErrImagePull",
    "ImagePullBackOff",
    "CreateContainerConfigError",
    "InvalidImageName",
    "RunContainerError",
    "Failed",
    "FailedMount",
    "FailedScheduling",
    "Unhealthy",
    "BackOff",
    "NodeLost",
    "NodeNotReady",
    "NodeRebooted",
}


@registry.register(
    ExtractorMetadata(
        extractor_id="kubernetes_extractor",
        version="1.0.0",
        source="kubernetes",
        produces_kinds=("generic_log_line",),
        description=(
            "Phase 2.3 — parses the JSON envelope emitted by "
            "KubernetesCollector into Observations of kind "
            "'generic_log_line' (source='kubernetes')."
        ),
    )
)
class KubernetesExtractor(BaseExtractor):
    EXTRACTOR_ID = "kubernetes_extractor"

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
        if payload.get("type") != "kubernetes_state":
            return observations

        namespace = payload.get("namespace") or payload.get(
            "namespaces_queried", [None]
        )[0]
        if not isinstance(namespace, str):
            namespace = None
        service_hint = payload.get("service")
        if not isinstance(service_hint, str):
            service_hint = None

        pods = payload.get("pods") or []
        events = payload.get("events") or []
        deployments = payload.get("deployments") or []

        # Track dedup keys (we want at most one observation per
        # pod-reason pair, per event name, per deployment).
        seen_keys: set = set()

        if isinstance(pods, list):
            for pod in pods:
                if not isinstance(pod, dict):
                    continue
                name = pod.get("name") if isinstance(pod.get("name"), str) else "<unknown>"
                ns = pod.get("namespace") if isinstance(pod.get("namespace"), str) else namespace
                restart_count = int(pod.get("restart_count") or 0)
                containers = pod.get("containers") or []
                if not isinstance(containers, list):
                    containers = []

                for container in containers:
                    if not isinstance(container, dict):
                        continue
                    reasons = _container_reasons(container)
                    for reason in reasons:
                        key = f"pod:{name}:{reason}"
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        observations.append(
                            self._build_pod_observation(
                                context=context,
                                pod_name=name,
                                namespace=ns,
                                container_name=container.get("name"),
                                reason=reason,
                                container=container,
                                restart_count=restart_count,
                                service_hint=service_hint,
                            )
                        )

                # Restart-count observation (deduped per pod).
                if restart_count > 0 and not any(
                    k.startswith(f"pod:{name}:restart:") for k in seen_keys
                ):
                    seen_keys.add(f"pod:{name}:restart:{restart_count}")
                    observations.append(
                        self._build_restart_observation(
                            context=context,
                            pod_name=name,
                            namespace=ns,
                            restart_count=restart_count,
                            service_hint=service_hint,
                        )
                    )

        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                reason = ev.get("reason")
                ev_type = ev.get("type")
                if (
                    isinstance(reason, str)
                    and reason in _INCIDENT_REASONS
                ):
                    name = ev.get("name") or ev.get("involved_name") or "<event>"
                    key = f"event:{name}:{reason}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    observations.append(
                        self._build_event_observation(
                            context=context,
                            event=ev,
                            namespace=ev.get("namespace") or namespace,
                            service_hint=service_hint,
                        )
                    )
                elif (
                    isinstance(ev_type, str)
                    and ev_type == "Warning"
                    and isinstance(reason, str)
                    and reason
                ):
                    # Generic Warning event — keep but dedup.
                    name = ev.get("name") or ev.get("involved_name") or "<event>"
                    key = f"event:{name}:Warning:{reason}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    observations.append(
                        self._build_event_observation(
                            context=context,
                            event=ev,
                            namespace=ev.get("namespace") or namespace,
                            service_hint=service_hint,
                        )
                    )

        if isinstance(deployments, list):
            for dep in deployments:
                if not isinstance(dep, dict):
                    continue
                unavailable = dep.get("replicas_unavailable")
                if (
                    isinstance(unavailable, int)
                    and unavailable > 0
                ):
                    name = dep.get("name") or "<deployment>"
                    key = f"deployment:{name}:unavailable:{unavailable}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    observations.append(
                        self._build_deployment_observation(
                            context=context,
                            deployment=dep,
                            namespace=dep.get("namespace") or namespace,
                            service_hint=service_hint,
                        )
                    )

        if not observations and namespace:
            # Soft-fallback: the namespace was queried but no incident
            # findings fired. Emit a single low-noise observation so
            # callers know the integration was reached but found nothing.
            observations.append(
                self._build_clean_observation(
                    context=context,
                    namespace=namespace,
                    pod_count=payload.get("pod_count") if isinstance(payload.get("pod_count"), int) else 0,
                    event_count=payload.get("event_count") if isinstance(payload.get("event_count"), int) else 0,
                    deployment_count=payload.get("deployment_count") if isinstance(payload.get("deployment_count"), int) else 0,
                    service_hint=service_hint,
                )
            )

        return observations

    # ------------------------------------------------------------------
    # Observation builders
    # ------------------------------------------------------------------
    def _build_pod_observation(
        self,
        *,
        context: ExtractionContext,
        pod_name: str,
        namespace: Optional[str],
        container_name: Optional[Any],
        reason: str,
        container: Dict[str, Any],
        restart_count: int,
        service_hint: Optional[str],
    ) -> Observation:
        data: Dict[str, Any] = {
            "kind": "kubernetes_pod_state",
            "pod": pod_name,
            "namespace": namespace,
            "container": container_name if isinstance(container_name, str) else None,
            "reason": reason,
            "restart_count": restart_count,
            "ready": bool(container.get("ready")),
        }
        exit_code = container.get("terminated_exit_code")
        if exit_code is not None:
            data["exit_code"] = exit_code
        msg = container.get("waiting_message") or container.get(
            "terminated_reason"
        )
        if isinstance(msg, str) and msg:
            data["message"] = msg[:256]
        timestamp = _first_of(
            container.get("running_started_at"),
            container.get("terminated_reason"),
        )
        extracted_at = _safe_iso(timestamp) or _iso_now()
        location = Location(
            step_name=f"kubernetes:pod:{pod_name}:{reason}",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(
                container, ensure_ascii=False, sort_keys=True
            )[:256],
            extracted_at=extracted_at,
            service=service_hint,
            resource=f"kubernetes:{namespace or '?'}/{pod_name}",
            normalized_value=reason,
            timestamp_known=bool(timestamp),
        )

    def _build_restart_observation(
        self,
        *,
        context: ExtractionContext,
        pod_name: str,
        namespace: Optional[str],
        restart_count: int,
        service_hint: Optional[str],
    ) -> Observation:
        data = {
            "kind": "kubernetes_restart_count",
            "pod": pod_name,
            "namespace": namespace,
            "restart_count": restart_count,
        }
        location = Location(
            step_name=f"kubernetes:pod:{pod_name}:restarts",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(
                {"pod": pod_name, "restart_count": restart_count},
                sort_keys=True,
            ),
            extracted_at=_iso_now(),
            service=service_hint,
            resource=f"kubernetes:{namespace or '?'}/{pod_name}",
            normalized_value=str(restart_count),
            timestamp_known=False,
        )

    def _build_event_observation(
        self,
        *,
        context: ExtractionContext,
        event: Dict[str, Any],
        namespace: Optional[str],
        service_hint: Optional[str],
    ) -> Observation:
        reason = event.get("reason") or "Event"
        msg = event.get("message") or ""
        involved_kind = event.get("involved_kind") or ""
        involved_name = event.get("involved_name") or ""
        count = event.get("count")
        data = {
            "kind": "kubernetes_event",
            "reason": reason,
            "namespace": namespace,
            "involved_kind": involved_kind,
            "involved_name": involved_name,
            "count": count if isinstance(count, int) else None,
        }
        if isinstance(msg, str) and msg:
            data["message"] = msg[:256]
        extracted_at = _safe_iso(
            event.get("last_timestamp") or event.get("first_timestamp")
        ) or _iso_now()
        location = Location(
            step_name=f"kubernetes:event:{event.get('name') or '<event>'}:{reason}",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(
                event, ensure_ascii=False, sort_keys=True
            )[:256],
            extracted_at=extracted_at,
            service=service_hint,
            resource=f"kubernetes:{namespace or '?'}/{involved_kind or 'event'}/{involved_name or ''}",
            normalized_value=reason,
            timestamp_known=bool(
                event.get("last_timestamp") or event.get("first_timestamp")
            ),
        )

    def _build_deployment_observation(
        self,
        *,
        context: ExtractionContext,
        deployment: Dict[str, Any],
        namespace: Optional[str],
        service_hint: Optional[str],
    ) -> Observation:
        unavailable = deployment.get("replicas_unavailable")
        ready = deployment.get("replicas_ready")
        desired = deployment.get("replicas_desired")
        data = {
            "kind": "kubernetes_deployment",
            "deployment": deployment.get("name"),
            "namespace": namespace,
            "replicas_desired": desired,
            "replicas_ready": ready,
            "replicas_unavailable": unavailable,
        }
        location = Location(
            step_name=f"kubernetes:deployment:{deployment.get('name') or '<deployment>'}",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(
                deployment, ensure_ascii=False, sort_keys=True
            )[:256],
            extracted_at=_iso_now(),
            service=service_hint,
            resource=f"kubernetes:{namespace or '?'}/deployment/{deployment.get('name') or ''}",
            normalized_value=str(unavailable or 0),
            timestamp_known=False,
        )

    def _build_clean_observation(
        self,
        *,
        context: ExtractionContext,
        namespace: str,
        pod_count: int,
        event_count: int,
        deployment_count: int,
        service_hint: Optional[str],
    ) -> Observation:
        data = {
            "kind": "kubernetes_namespace_clean",
            "namespace": namespace,
            "pod_count": pod_count,
            "event_count": event_count,
            "deployment_count": deployment_count,
        }
        location = Location(
            step_name=f"kubernetes:namespace:{namespace}:clean",
        )
        return self.build_observation(
            context=context,
            kind="generic_log_line",
            location=location,
            data=data,
            raw_reference=json.dumps(data, sort_keys=True),
            extracted_at=_iso_now(),
            service=service_hint,
            resource=f"kubernetes:{namespace}",
            normalized_value="clean",
            timestamp_known=False,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _container_reasons(container: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    waiting = container.get("waiting_reason")
    if isinstance(waiting, str) and waiting in _INCIDENT_REASONS:
        reasons.append(waiting)
    terminated = container.get("terminated_reason")
    if isinstance(terminated, str) and terminated in _INCIDENT_REASONS:
        reasons.append(terminated)
    return reasons


def _first_of(*values: Any) -> Optional[str]:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _safe_iso(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


__all__ = ["KubernetesExtractor"]
