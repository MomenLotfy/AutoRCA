"""
engine/remediation_engine.py
-----------------------------------------------------------------------------
RemediationEngine — يحوّل fix_hints النصّي الثابت في rules.config.json إلى
سياق إصلاح منظم: {action, steps, validation, rollback}.

مبدأ صارم:
- الـ context يُبنى بشكل حتمي بالكامل من:
  * failure_type_id المختار
  * fix_hint من rules.config.json (placeholder حتمي)
  * قائمة الـ supporting_evidence (لاستخراج رموز مثل اسم المتغير، اسم الـ module)
  * environment (لتفريع validation وrollback)
- الـ LLM لا يُسمح له باختراع خطوات جديدة — هو فقط يحول هذا الـ context
  إلى شرح طبيعي. أي اختراع من LLM للـ action يتجاوز LLM boundary.

لا hardcoded قيم خاصة بـ target repository.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


VALID_ACTION_TYPES: tuple[str, ...] = (
    "restore_missing_environment_variable",
    "reinstall_missing_dependency",
    "resolve_port_conflict",
    "investigate_runtime_error",
)


@dataclass(frozen=True)
class RemediationContext:
    analysis_id: str
    failure_type_id: str
    action: str
    description: str
    steps: List[str]
    validation: List[str]
    rollback: List[str]
    target_symbols: List[str] = field(default_factory=list)
    environment: str = "unknown"

    def to_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "failure_type_id": self.failure_type_id,
            "action": self.action,
            "description": self.description,
            "steps": list(self.steps),
            "validation": list(self.validation),
            "rollback": list(self.rollback),
            "target_symbols": list(self.target_symbols),
            "environment": self.environment,
        }


def _extract_env_var_names(evidence_list: List[dict]) -> List[str]:
    found: List[str] = []
    for evidence in evidence_list:
        data = evidence.get("data") or {}
        key = data.get("key")
        if isinstance(key, str) and key.strip():
            found.append(key.strip())
    return list(dict.fromkeys(found))


def _extract_module_names(evidence_list: List[dict]) -> List[str]:
    found: List[str] = []
    for evidence in evidence_list:
        data = evidence.get("data") or {}
        module = data.get("module")
        if isinstance(module, str) and module.strip():
            found.append(module.strip())
    return list(dict.fromkeys(found))


def _extract_ports(evidence_list: List[dict]) -> List[int]:
    found: List[int] = []
    for evidence in evidence_list:
        data = evidence.get("data") or {}
        port = data.get("port")
        if isinstance(port, int):
            found.append(port)
    return list(dict.fromkeys(found))


class RemediationEngine:
    def __init__(self, fix_hints: Dict[str, str]) -> None:
        self._fix_hints = fix_hints

    def build(
        self,
        *,
        analysis_id: str,
        failure_type_id: str,
        evidence_list: List[dict],
        environment: str = "unknown",
    ) -> RemediationContext:
        base_hint = self._fix_hints.get(failure_type_id, "")

        if failure_type_id == "FT001":
            return self._for_missing_env(analysis_id, evidence_list, environment, base_hint)
        if failure_type_id == "FT002":
            return self._for_missing_dependency(analysis_id, evidence_list, environment, base_hint)
        if failure_type_id == "FT003":
            return self._for_port_conflict(analysis_id, evidence_list, environment, base_hint)

        return RemediationContext(
            analysis_id=analysis_id,
            failure_type_id=failure_type_id,
            action="investigate_runtime_error",
            description=base_hint or "Investigate the runtime failure using collected evidence.",
            steps=[
                "Inspect the supporting evidence references for the failing call site.",
                "Re-run the application in a controlled environment to reproduce.",
            ],
            validation=[
                "Verify the application reaches a healthy state after applying changes.",
            ],
            rollback=[
                "Revert the change that introduced the failure if validation fails.",
            ],
            target_symbols=[],
            environment=environment,
        )

    # ------------------------------------------------------------------
    # Specific builders — كل واحد مسؤول عن failure_type واحد فقط
    # ------------------------------------------------------------------

    def _for_missing_env(
        self,
        analysis_id: str,
        evidence_list: List[dict],
        environment: str,
        base_hint: str,
    ) -> RemediationContext:
        symbols = _extract_env_var_names(evidence_list)
        action = "restore_missing_environment_variable"

        steps: List[str] = []
        for sym in symbols:
            steps.append(
                f"Restore environment variable '{sym}' in the configuration "
                f"used by {environment} (e.g., .env, deployment manifest, secrets store)."
            )
        if not steps:
            steps.append(
                "Inspect supporting evidence to determine which environment variable "
                "is required at runtime and restore it in the configuration."
            )

        validation: List[str] = []
        for sym in symbols:
            validation.append(
                f"Confirm '{sym}' is present in the application process environment "
                f"(e.g., printenv, /proc/self/environ, Kubernetes downward API)."
            )
        validation.append(
            "Restart the service and verify it starts without the previous KeyError."
        )

        rollback: List[str] = []
        for sym in symbols:
            rollback.append(
                f"Roll back the configuration change that removed '{sym}' from source control."
            )
        rollback.append("Redeploy the previous known-good container image if needed.")

        return RemediationContext(
            analysis_id=analysis_id,
            failure_type_id="FT001",
            action=action,
            description=base_hint or "Restore the missing environment variable and redeploy the service.",
            steps=steps,
            validation=validation,
            rollback=rollback,
            target_symbols=symbols,
            environment=environment,
        )

    def _for_missing_dependency(
        self,
        analysis_id: str,
        evidence_list: List[dict],
        environment: str,
        base_hint: str,
    ) -> RemediationContext:
        symbols = _extract_module_names(evidence_list)
        action = "reinstall_missing_dependency"

        steps: List[str] = []
        for sym in symbols:
            steps.append(
                f"Add '{sym}' back to the dependency manifest "
                f"(requirements.txt / pyproject.toml / package.json) and pin its version."
            )
            steps.append(f"Reinstall dependencies in the build stage for {environment}.")
        if not steps:
            steps.append(
                "Inspect supporting evidence to determine which module is missing "
                "and add it to the dependency manifest."
            )

        validation: List[str] = []
        for sym in symbols:
            validation.append(
                f"Verify '{sym}' is importable inside the container (e.g., "
                f"`python -c 'import {sym}'` or `node -e \"require('{sym}')\"`)."
            )
        validation.append("Rebuild the container image and confirm startup succeeds.")

        rollback: List[str] = [
            "Revert the dependency manifest change and rebuild the previous known-good image.",
        ]

        return RemediationContext(
            analysis_id=analysis_id,
            failure_type_id="FT002",
            action=action,
            description=base_hint or "Reinstall the missing package and ensure it is pinned in the dependency manifest, then redeploy.",
            steps=steps,
            validation=validation,
            rollback=rollback,
            target_symbols=symbols,
            environment=environment,
        )

    def _for_port_conflict(
        self,
        analysis_id: str,
        evidence_list: List[dict],
        environment: str,
        base_hint: str,
    ) -> RemediationContext:
        ports = _extract_ports(evidence_list)
        action = "resolve_port_conflict"

        steps: List[str] = []
        for port in ports:
            steps.append(
                f"Identify the process or container currently bound to port {port} "
                f"(e.g., `ss -tlnp | grep {port}` or `lsof -i :{port}`)."
            )
            steps.append(
                f"Either stop the conflicting holder of port {port}, or reassign "
                f"the application to a free port and update the deployment config."
            )
        if not steps:
            steps.append(
                "Inspect supporting evidence to determine which port is conflicting "
                "and stop or reassign the conflicting process/container."
            )

        validation: List[str] = [
            "Confirm no process is bound to the target port before redeploying.",
            "Restart the application and confirm the bind() call succeeds.",
        ]

        rollback: List[str] = [
            "Revert the port-mapping or port-binding change that introduced the conflict.",
        ]

        return RemediationContext(
            analysis_id=analysis_id,
            failure_type_id="FT003",
            action=action,
            description=base_hint or "Check for an existing process or container already bound to this port, stop it or reassign the port, then redeploy.",
            steps=steps,
            validation=validation,
            rollback=rollback,
            target_symbols=[str(p) for p in ports],
            environment=environment,
        )
