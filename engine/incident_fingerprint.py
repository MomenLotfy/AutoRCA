"""
engine/incident_fingerprint.py
-----------------------------------------------------------------------------
IncidentFingerprint — تمثيل هيكلي مختصر لخصائص الحادثة.

لا يُستخدم للإجابة عن الحادثة (لا hardcoded answer) — بل لوصف شكلها
المنظم. الهدف: تمكين المطابقة المستقبلية بين الحوادث المتشابهة.

الحقول:
- failure_category: الفئة من taxonomy (environment / build / network / ...)
- failure_type: نوع الفشل (missing_env / missing_dependency / port_conflict / ...)
- exception_type: اسم الـ exception المُشتق (KeyError / ModuleNotFoundError / OSError / None)
- affected_service: اسم الخدمة المُستنتج (من commit SHA إن وُجد، أو 'unknown')
- failure_stage: مرحلة الفشل (git_diff / traceback / docker_output / application_startup / ci)
- configuration_area: مجال التكوين (env_file / dependency_manifest / port_binding / unknown)
- related_change_type: نوع التغيير المرتبط (env_var_removal / dependency_removal / port_change / unknown)
- environment: بيئة التشغيل (local / staging / production)
- runtime_type: نوع runtime (python / node / unknown)
- signature_keys: قائمة مفاتيح مميزة للبحث/المطابقة

كل الحقول تُحسب من Observation/Evidence حقيقية.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from extractors.base import Observation


VALID_CATEGORIES: tuple[str, ...] = (
    "environment",
    "build",
    "network",
    "runtime",
    "testing",
    "security",
    "database",
)

VALID_STAGES: tuple[str, ...] = (
    "git_diff",
    "traceback",
    "docker_output",
    "application_startup",
    "ci_log",
    "test_output",
)

VALID_CONFIG_AREAS: tuple[str, ...] = (
    "env_file",
    "dependency_manifest",
    "port_binding",
    "unknown",
)

VALID_RELATED_CHANGE_TYPES: tuple[str, ...] = (
    "env_var_removal",
    "env_var_addition",
    "dependency_removal",
    "dependency_addition",
    "port_change",
    "unknown",
)


class IncidentFingerprintError(ValueError):
    pass


@dataclass(frozen=True)
class IncidentFingerprint:
    analysis_id: str
    failure_category: str
    failure_type: str
    exception_type: Optional[str]
    affected_service: str
    failure_stage: str
    configuration_area: str
    related_change_type: str
    environment: str
    runtime_type: str
    signature_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "failure_category": self.failure_category,
            "failure_type": self.failure_type,
            "exception_type": self.exception_type,
            "affected_service": self.affected_service,
            "failure_stage": self.failure_stage,
            "configuration_area": self.configuration_area,
            "related_change_type": self.related_change_type,
            "environment": self.environment,
            "runtime_type": self.runtime_type,
            "signature_keys": list(self.signature_keys),
        }


_FAILURE_TYPE_TO_CATEGORY: Dict[str, str] = {
    "FT001": "environment",
    "FT002": "build",
    "FT003": "network",
    "FT004": "runtime",
    "FT005": "build",
    "FT006": "testing",
    "FT007": "runtime",
    "FT008": "security",
    "FT009": "database",
    "FT010": "security",
}

_FAILURE_TYPE_TO_CONFIG_AREA: Dict[str, str] = {
    "FT001": "env_file",
    "FT002": "dependency_manifest",
    "FT003": "port_binding",
}

_FAILURE_TYPE_TO_RUNTIME: Dict[str, str] = {
    "FT001": "python",
    "FT002": "python",
    "FT003": "python",
}


def _exception_name_from_observation(obs: Observation) -> Optional[str]:
    kind = obs.kind
    if kind == "key_error":
        return "KeyError"
    if kind == "module_not_found_error":
        return "ModuleNotFoundError"
    if kind == "address_in_use_error":
        return "OSError"
    return None


def _detect_related_change_type(observations: List[Observation]) -> str:
    """
    يشتق نوع التغيير المرتبط من Observations:
    - إذا وُجد diff_removed_line في .env → env_var_removal
    - إذا وُجد diff_removed_line في dependency manifest → dependency_removal
    - إذا وُجد address_in_use_error → port_change
    """
    has_env_removal = False
    has_dependency_removal = False
    has_port = False

    for obs in observations:
        if obs.kind != "diff_removed_line":
            continue
        file_path = obs.location.file or ""
        basename = file_path.split("/")[-1] if file_path else ""
        if basename == ".env" or basename.startswith(".env."):
            has_env_removal = True
        elif basename in {"requirements.txt", "Pipfile", "pyproject.toml", "package.json"}:
            has_dependency_removal = True

    for obs in observations:
        if obs.kind == "address_in_use_error":
            has_port = True

    if has_env_removal:
        return "env_var_removal"
    if has_dependency_removal:
        return "dependency_removal"
    if has_port:
        return "port_change"
    return "unknown"


def _detect_failure_stage(observations: List[Observation]) -> str:
    """
    يختار أقدم مرحلة فُشل حقيقية من Observations حسب أولوية:
    traceback > docker_output > git_diff > ci_log > test_output.
    """
    priority = ["traceback", "docker_output", "git_diff", "ci_log", "test_output"]
    sources = {o.source for o in observations}
    for stage in priority:
        if stage in sources:
            return stage
    return "unknown"


def _detect_runtime_type(observations: List[Observation], failure_type_id: Optional[str]) -> str:
    """
    يبسط تخمين الـ runtime: لو failure_type_id معروف استخدم الجدول،
    وإلا حاول استخراجه من raw_reference.
    """
    if failure_type_id in _FAILURE_TYPE_TO_RUNTIME:
        return _FAILURE_TYPE_TO_RUNTIME[failure_type_id]
    for obs in observations:
        ref = obs.raw_reference or ""
        if "Cannot find module" in ref:
            return "node"
        if "ModuleNotFoundError" in ref or "ImportError" in ref or "KeyError" in ref:
            return "python"
    return "unknown"


def _signature_keys(failure_type: str, exception_type: Optional[str], env_vars: List[str], modules: List[str]) -> List[str]:
    parts = [failure_type]
    if exception_type:
        parts.append(exception_type)
    for v in env_vars:
        parts.append(f"env:{v}")
    for m in modules:
        parts.append(f"module:{m}")
    return parts


_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class IncidentFingerprintBuilder:
    def __init__(self) -> None:
        pass

    def build(
        self,
        *,
        analysis_id: str,
        observations: List[Observation],
        failure_type_id: Optional[str],
        environment: str,
        affected_service: Optional[str] = None,
    ) -> IncidentFingerprint:
        failure_category = _FAILURE_TYPE_TO_CATEGORY.get(failure_type_id or "", "runtime")
        failure_type = failure_type_id or "unknown"

        exception_type: Optional[str] = None
        for obs in observations:
            name = _exception_name_from_observation(obs)
            if name is not None:
                exception_type = name
                break

        failure_stage = _detect_failure_stage(observations)
        config_area = _FAILURE_TYPE_TO_CONFIG_AREA.get(failure_type_id or "", "unknown")
        related_change_type = _detect_related_change_type(observations)
        runtime_type = _detect_runtime_type(observations, failure_type_id)

        env_vars: List[str] = []
        modules: List[str] = []
        for obs in observations:
            if obs.kind == "key_error" and isinstance(obs.data.get("key"), str):
                k = obs.data["key"].upper()
                if _VAR_RE.match(k):
                    env_vars.append(k)
            if obs.kind == "module_not_found_error" and isinstance(obs.data.get("module"), str):
                modules.append(obs.data["module"])

        signature = _signature_keys(failure_type, exception_type, sorted(set(env_vars)), sorted(set(modules)))

        return IncidentFingerprint(
            analysis_id=analysis_id,
            failure_category=failure_category,
            failure_type=failure_type,
            exception_type=exception_type,
            affected_service=affected_service or "unknown",
            failure_stage=failure_stage,
            configuration_area=config_area,
            related_change_type=related_change_type,
            environment=environment,
            runtime_type=runtime_type,
            signature_keys=signature,
        )
