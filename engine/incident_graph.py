"""
engine/incident_graph.py
-----------------------------------------------------------------------------
IncidentGraph — تمثيل سببي منظم للحادثة كـ nodes و edges.

العقد (nodes) مستوحاة من التصنيف الرسمي في AutoRCA taxonomy، لكنها لا
تحتوي أي قيم hardcoded خاصة بـ target repository — كل عقدة تُشتق من
observation حقيقي أو من failure_type مُختار.

أنواع العقد:
- git_change: تغيير في git (commit, diff)
- configuration_file: ملف تكوين متأثر
- missing_environment_variable: متغير بيئة مفقود
- missing_dependency: اعتماد مفقود
- runtime_error: خطأ runtime (KeyError, ModuleNotFoundError, etc.)
- port_binding: محاولة bind لمنفذ
- service_startup: بدء تشغيل خدمة
- container_failure: فشل container
- application_startup_failure: فشل بدء التطبيق

الحواف (edges) مستوحاة من CorrelationEngine (caused_by, references, modifies, etc.)
و من طبيعة الـ lifecycle (leads_to, triggers).
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from engine.correlation_engine import CorrelationEngine, CorrelationGraph, _extract_env_var_name, _extract_module_name
from extractors.base import Observation


VALID_NODE_TYPES: tuple[str, ...] = (
    "git_change",
    "configuration_file",
    "missing_environment_variable",
    "missing_dependency",
    "runtime_error",
    "port_binding",
    "service_startup",
    "container_failure",
    "application_startup_failure",
)

VALID_EDGE_TYPES: tuple[str, ...] = (
    "leads_to",
    "triggers",
    "caused_by",
    "modifies",
    "references",
)


class IncidentGraphError(ValueError):
    pass


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    label: str
    description: str
    related_observation_ids: List[str] = field(default_factory=list)
    related_evidence_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "description": self.description,
            "related_observation_ids": list(self.related_observation_ids),
            "related_evidence_ids": list(self.related_evidence_ids),
        }


@dataclass(frozen=True)
class GraphEdge:
    source_node_id: str
    target_node_id: str
    relation: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation": self.relation,
        }


@dataclass(frozen=True)
class IncidentGraph:
    analysis_id: str
    nodes: List[GraphNode]
    edges: List[GraphEdge]

    def to_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
        }


class IncidentGraphBuilder:
    """
    يبني IncidentGraph من:
    - CorrelationGraph
    - قائمة Observations الحقيقية (لتفاصيل runtime_error و configuration_file)
    - failure_type_id و label (لإنشاء عقدة application-level)
    - commit_sha (اختياري) — لإنشاء عقدة git_change مجمّعة

    يبني الـ graph deterministic — لا يولّد عقدًا بدون evidence.
    """

    def __init__(self) -> None:
        self._nodes: List[GraphNode] = []
        self._edges: List[GraphEdge] = []
        self._node_counter: int = 0
        self._obs_to_node: Dict[str, str] = {}
        self._edge_signatures: Set[tuple[str, str, str]] = set()

    def build(
        self,
        *,
        analysis_id: str,
        correlation: CorrelationGraph,
        observations: List[Observation],
        selected_failure_type_id: Optional[str],
        selected_failure_label: Optional[str],
        commit_sha: Optional[str] = None,
    ) -> IncidentGraph:
        self._reset()
        obs_by_id = {o.id: o for o in observations}
        runtime_node_ids: List[str] = []
        config_node_ids: List[str] = []
        commit_node_ids: List[str] = []

        # 1) عقدة git_change عامة (واحدة على الأكثر) عند توفر commit_sha
        if commit_sha:
            git_node_id = self._new_node(
                type="git_change",
                label=f"Git change {commit_sha[:12]}",
                description=f"Commit {commit_sha} introduced a change correlated with the incident.",
            )
            commit_node_ids.append(git_node_id)

        # 2) من CorrelationGraph: حواف بين Observations
        for edge in correlation.edges:
            src_id = self._ensure_observation_node(
                edge.source_observation_id, obs_by_id, runtime_node_ids
            )
            tgt_id = self._ensure_observation_node(
                edge.target_observation_id, obs_by_id, runtime_node_ids
            )
            if edge.relation == "modifies":
                self._safe_add_edge(src_id, tgt_id, "modifies")
            elif edge.relation == "references":
                self._safe_add_edge(src_id, tgt_id, "references")
            elif edge.relation == "caused_by":
                self._safe_add_edge(src_id, tgt_id, "leads_to")

        # 3) configuration_file nodes لكل ملف ذُكر في observations
        for obs in observations:
            if obs.kind not in ("diff_removed_line", "diff_added_line"):
                continue
            file_path = obs.location.file
            if not file_path:
                continue
            node_id = self._new_node(
                type="configuration_file",
                label=f"Configuration file {file_path.split('/')[-1]}",
                description=f"File path: {file_path}",
                related_observation_ids=[obs.id],
            )
            config_node_ids.append(node_id)
            # ربط بـ runtime errors عبر الـ symbol المشترك
            self._link_via_symbols(obs, node_id)

        # 4) عقدة application-level
        app_node_id: Optional[str] = None
        if selected_failure_type_id and selected_failure_label:
            app_node_id = self._build_application_node(
                selected_failure_type_id, selected_failure_label
            )
            # runtime_errors -> app_node (triggers)
            for nid in runtime_node_ids:
                self._safe_add_edge(nid, app_node_id, "triggers")
            # configuration_files -> app_node (leads_to)
            for nid in config_node_ids:
                self._safe_add_edge(nid, app_node_id, "leads_to")
            # app_node -> git_change كـ caused_by (الـ commit غيّر التكوين مما تسبب في app failure)
            for nid in commit_node_ids:
                self._safe_add_edge(app_node_id, nid, "caused_by")

        return IncidentGraph(
            analysis_id=analysis_id,
            nodes=list(self._nodes),
            edges=list(self._edges),
        )

    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self._nodes = []
        self._edges = []
        self._node_counter = 0
        self._obs_to_node = {}
        self._edge_signatures = set()

    def _new_node(
        self,
        *,
        type: str,
        label: str,
        description: str,
        related_observation_ids: Optional[List[str]] = None,
        related_evidence_ids: Optional[List[str]] = None,
    ) -> str:
        if type not in VALID_NODE_TYPES:
            raise IncidentGraphError(
                f"نوع عقدة غير صالح '{type}'. المسموح: {VALID_NODE_TYPES}"
            )
        self._node_counter += 1
        nid = f"N{self._node_counter}"
        self._nodes.append(
            GraphNode(
                id=nid,
                type=type,
                label=label,
                description=description,
                related_observation_ids=list(related_observation_ids or []),
                related_evidence_ids=list(related_evidence_ids or []),
            )
        )
        return nid

    def _ensure_observation_node(
        self,
        obs_id: str,
        obs_by_id: Dict[str, Observation],
        runtime_node_ids: List[str],
    ) -> str:
        if obs_id in self._obs_to_node:
            return self._obs_to_node[obs_id]
        obs = obs_by_id.get(obs_id)
        if obs is None:
            label = f"Runtime error (observation {obs_id})"
            description = "Stub node created from correlation edge without observation context."
        else:
            label = self._humanize_runtime_label(obs)
            description = obs.raw_reference[:300] if obs.raw_reference else label
        nid = self._new_node(
            type="runtime_error",
            label=label,
            description=description,
            related_observation_ids=[obs_id],
        )
        runtime_node_ids.append(nid)
        self._obs_to_node[obs_id] = nid
        return nid

    def _link_via_symbols(self, obs: Observation, file_node_id: str) -> None:
        if obs.kind != "diff_removed_line":
            return
        line_content = obs.data.get("line_content")
        if not isinstance(line_content, str):
            return
        var = _extract_env_var_name(line_content)
        if var:
            for rt_node in self._nodes:
                if rt_node.type != "runtime_error":
                    continue
                if var in rt_node.label.upper():
                    self._safe_add_edge(file_node_id, rt_node.id, "leads_to")
        mod = _extract_module_name(line_content)
        if mod:
            for rt_node in self._nodes:
                if rt_node.type != "runtime_error":
                    continue
                if mod in rt_node.label:
                    self._safe_add_edge(file_node_id, rt_node.id, "leads_to")

    @staticmethod
    def _humanize_runtime_label(obs: Observation) -> str:
        kind = obs.kind
        if kind == "key_error" and "key" in obs.data:
            return f"KeyError('{obs.data['key']}')"
        if kind == "module_not_found_error" and "module" in obs.data:
            return f"ModuleNotFoundError('{obs.data['module']}')"
        if kind == "address_in_use_error":
            port = obs.data.get("port")
            return f"PortConflict({port})" if port is not None else "PortConflict"
        if kind in ("diff_removed_line", "diff_added_line"):
            file_part = obs.location.file.split("/")[-1] if obs.location.file else "?"
            return f"{kind.split('_')[0]}_{file_part}"
        return kind

    def _build_application_node(self, failure_type_id: str, label: str) -> str:
        if failure_type_id == "FT001":
            return self._new_node(
                type="missing_environment_variable",
                label="Missing environment variable",
                description=f"A required environment variable is missing at runtime (failure_type_id={failure_type_id}).",
            )
        if failure_type_id == "FT002":
            return self._new_node(
                type="missing_dependency",
                label="Missing dependency",
                description=f"A required package/module is not installed or missing from the manifest (failure_type_id={failure_type_id}).",
            )
        if failure_type_id == "FT003":
            return self._new_node(
                type="port_binding",
                label="Port bind failure",
                description=f"Application failed to bind because target port is already in use (failure_type_id={failure_type_id}).",
            )
        return self._new_node(
            type="application_startup_failure",
            label=label.replace("_", " "),
            description=f"Application startup failure ({failure_type_id}).",
        )

    def _safe_add_edge(self, source: str, target: str, relation: str) -> None:
        if relation not in VALID_EDGE_TYPES:
            raise IncidentGraphError(f"نوع حافة غير صالح '{relation}'.")
        if source == target:
            return
        sig = (source, target, relation)
        if sig in self._edge_signatures:
            return
        self._edge_signatures.add(sig)
        self._edges.append(
            GraphEdge(source_node_id=source, target_node_id=target, relation=relation)
        )
