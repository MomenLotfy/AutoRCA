"""
engine/correlation_engine.py
-----------------------------------------------------------------------------
محرك الارتباط (Correlation Engine) — يربط بين Observations المختلفة عبر
evidence IDs وعلاقات سببية منظمة (modifies / references / caused_by /
supports / contradicts / occurred_before / affects_service).

لا يبحث في نصوص خام عشوائيًا. كل correlation يُشتق من نوع Observation
والـ data field داخله (مثلاً diff_removed_line مع line_content يطابق
KEY=VALUE في .env، أو key_error مع data.key == اسم المتغير المُزال).

Relationships المتاحة:
- modifies: تغيير في Git أضاف/أزال سطرًا في ملف تكوين
- references: دليل runtime يشير إلى معرّف (variable name, module name)
- caused_by: ارتباط سببي مُشتق بين observation وآخر
- supports: evidence يُؤيّد hypothesis
- contradicts: evidence يُعاكس hypothesis
- occurred_before: ترتيب زمني
- occurred_after: ترتيب زمني
- affects_service: تأثير على خدمة معيّنة
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from extractors.base import Observation


VALID_RELATION_TYPES: tuple[str, ...] = (
    "modifies",
    "references",
    "caused_by",
    "supports",
    "contradicts",
    "occurred_before",
    "occurred_after",
    "affects_service",
)


class CorrelationEngineError(ValueError):
    pass


@dataclass(frozen=True)
class CorrelationEdge:
    source_observation_id: str
    target_observation_id: str
    relation: str
    confidence: float  # 0..1 — مدى قوة الارتباط المُشتق
    rationale: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "source_observation_id": self.source_observation_id,
            "target_observation_id": self.target_observation_id,
            "relation": self.relation,
            "confidence": round(self.confidence, 4),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CorrelationGraph:
    analysis_id: str
    edges: List[CorrelationEdge]

    def to_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "edges": [e.to_dict() for e in self.edges],
            "edge_count": len(self.edges),
        }


def _normalize_name(name: str) -> str:
    """Normalize variable/module names for cross-source comparison."""
    return name.strip().strip("'\"").upper()


_ENV_VAR_LINE_PATTERN = re.compile(
    r"^\s*([A-Z_][A-Z0-9_]*)\s*="
)
_MODULE_LINE_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*([~=<>!]=|==)"
)
_FILE_PATH_BASENAME = re.compile(r"([^/]+)$")


def _extract_env_var_name(line_content: str) -> Optional[str]:
    if not line_content:
        return None
    match = _ENV_VAR_LINE_PATTERN.match(line_content)
    if match:
        return _normalize_name(match.group(1))
    return None


def _extract_module_name(line_content: str) -> Optional[str]:
    if not line_content:
        return None
    match = _MODULE_LINE_PATTERN.match(line_content)
    if match:
        return match.group(1)
    return None


def _is_env_file(path: Optional[str]) -> bool:
    if not path:
        return False
    basename = _FILE_PATH_BASENAME.search(path)
    if not basename:
        return False
    name = basename.group(1)
    return name == ".env" or name.startswith(".env.")


def _is_dependency_manifest(path: Optional[str]) -> bool:
    if not path:
        return False
    basename = _FILE_PATH_BASENAME.search(path)
    if not basename:
        return False
    name = basename.group(1)
    return name in {"requirements.txt", "Pipfile", "pyproject.toml", "package.json"}


class CorrelationEngine:
    """
    يبني CorrelationGraph من قائمة Observations حصرًا.

    العلاقات المُستخرجة:
    1. diff_removed_line في ملف .env يُساوي key_error runtime
       → edge (modifies, references, caused_by) — confidence عالية
    2. diff_removed_line في dependency manifest يُساوي module_not_found_error
       → edge (modifies, references, caused_by) — confidence عالية
    3. كل Observations لنفس analysis_id: ترتيب زمني occurred_before /
       occurred_after مُشتق من extracted_at
    """

    def __init__(self) -> None:
        self._edges: List[CorrelationEdge] = []

    def correlate(
        self,
        analysis_id: str,
        observations: List[Observation],
    ) -> CorrelationGraph:
        self._edges = []

        # 1) بناء فهرس: env variable name -> list of observations (key_error, removed .env lines)
        env_runtime_obs: Dict[str, List[Observation]] = {}
        env_diff_obs: Dict[str, List[Observation]] = {}

        # 2) module name -> observations
        module_runtime_obs: Dict[str, List[Observation]] = {}
        module_diff_obs: Dict[str, List[Observation]] = {}

        for obs in observations:
            if obs.kind == "key_error" and isinstance(obs.data.get("key"), str):
                key = _normalize_name(obs.data["key"])
                env_runtime_obs.setdefault(key, []).append(obs)
            elif obs.kind == "diff_removed_line" and isinstance(obs.data.get("line_content"), str):
                if _is_env_file(obs.location.file):
                    var_name = _extract_env_var_name(obs.data["line_content"])
                    if var_name:
                        env_diff_obs.setdefault(var_name, []).append(obs)
                if _is_dependency_manifest(obs.location.file):
                    mod_name = _extract_module_name(obs.data["line_content"])
                    if mod_name:
                        module_diff_obs.setdefault(mod_name, []).append(obs)
            elif obs.kind == "module_not_found_error" and isinstance(obs.data.get("module"), str):
                mod_name = obs.data["module"]
                module_runtime_obs.setdefault(mod_name, []).append(obs)

        # 3) Env variable correlation
        for var_name, runtime_list in env_runtime_obs.items():
            diff_list = env_diff_obs.get(var_name, [])
            for rt_obs in runtime_list:
                for df_obs in diff_list:
                    self._add_edge(
                        source=df_obs.id,
                        target=rt_obs.id,
                        relation="caused_by",
                        confidence=0.95,
                        rationale=(
                            f"Removed env line '{var_name}=...' from {df_obs.location.file} "
                            f"correlates with runtime KeyError for the same variable."
                        ),
                    )
                    self._add_edge(
                        source=df_obs.id,
                        target=rt_obs.id,
                        relation="modifies",
                        confidence=0.95,
                        rationale=f"Git diff removed the definition of '{var_name}'.",
                    )
                    self._add_edge(
                        source=rt_obs.id,
                        target=df_obs.id,
                        relation="references",
                        confidence=0.90,
                        rationale=f"Runtime KeyError references variable '{var_name}'.",
                    )

        # 4) Module correlation
        for mod_name, runtime_list in module_runtime_obs.items():
            diff_list = module_diff_obs.get(mod_name, [])
            for rt_obs in runtime_list:
                for df_obs in diff_list:
                    self._add_edge(
                        source=df_obs.id,
                        target=rt_obs.id,
                        relation="caused_by",
                        confidence=0.95,
                        rationale=(
                            f"Removed dependency line for '{mod_name}' from {df_obs.location.file} "
                            f"correlates with ModuleNotFoundError."
                        ),
                    )
                    self._add_edge(
                        source=df_obs.id,
                        target=rt_obs.id,
                        relation="modifies",
                        confidence=0.95,
                        rationale=f"Git diff removed dependency '{mod_name}'.",
                    )
                    self._add_edge(
                        source=rt_obs.id,
                        target=df_obs.id,
                        relation="references",
                        confidence=0.90,
                        rationale=f"Runtime ModuleNotFoundError references '{mod_name}'.",
                    )

        # 5) Temporal ordering — extracted_at لكل observation
        self._build_temporal_edges(observations)

        # 6) service-affects: address_in_use_error دائمًا affects_service (لأن الـ container فشل)
        for obs in observations:
            if obs.kind == "address_in_use_error":
                self._add_edge(
                    source=obs.id,
                    target=obs.id,
                    relation="affects_service",
                    confidence=1.0,
                    rationale="Port bind failure prevents the service from accepting traffic.",
                )

        return CorrelationGraph(analysis_id=analysis_id, edges=list(self._edges))

    def _add_edge(
        self,
        *,
        source: str,
        target: str,
        relation: str,
        confidence: float,
        rationale: str,
    ) -> None:
        if relation not in VALID_RELATION_TYPES:
            raise CorrelationEngineError(
                f"علاقة غير صالحة '{relation}'. المسموح: {VALID_RELATION_TYPES}"
            )
        # self-loop للـ affects_service فقط — منع أي self-loop آخر
        if source == target and relation != "affects_service":
            return
        self._edges.append(
            CorrelationEdge(
                source_observation_id=source,
                target_observation_id=target,
                relation=relation,
                confidence=confidence,
                rationale=rationale,
            )
        )

    def _build_temporal_edges(self, observations: List[Observation]) -> None:
        """ترتيب زمني: ربط كل observation بالأحدث التالي إذا كان extracted_at معروفًا."""
        with_ts = [o for o in observations if o.extracted_at]
        if len(with_ts) < 2:
            return
        with_ts.sort(key=lambda o: o.extracted_at)
        for i in range(len(with_ts) - 1):
            self._add_edge(
                source=with_ts[i].id,
                target=with_ts[i + 1].id,
                relation="occurred_before",
                confidence=1.0,
                rationale=(
                    f"Observation {with_ts[i].id} (extracted_at={with_ts[i].extracted_at}) "
                    f"occurred before {with_ts[i + 1].id} (extracted_at={with_ts[i + 1].extracted_at})."
                ),
            )
