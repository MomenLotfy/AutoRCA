"""
reporting/incident_report_renderer.py
-----------------------------------------------------------------------------
يحوّل PipelineResult لتقرير نصي مقروء بالبشر، بنفس الشكل المتفق عليه
(Root Cause / Confidence / Supporting Evidence / Related Change /
Recommended Fix). التنسيق حتمي بالكامل — لا يستدعي أي LLM.

Supporting Evidence هنا نص مقروء (مصدر الدليل + raw_reference)، مبني من
evidence_list الفعلي في PipelineResult — مش IDs مجردة، عشان يقرب لشكل
تقرير بشري حقيقي.

حقل "Recommended Fix" مصدره fix_hints في rules.config.json (نص ثابت
مكتوب مسبقًا)، ومُعلَّم صراحة في التقرير نفسه كـ placeholder لحد ما يتم
دمج LLM حقيقي يولّد شرح وخطوات مخصصة للحادثة.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Dict, List, Optional

from engine.rule_engine import Hypothesis
from pipeline import PipelineResult

_SOURCE_LABELS = {
    "traceback": "Application traceback",
    "git_diff": "Git diff",
    "docker_output": "Docker output",
    "ci_log": "CI log",
    "test_output": "Test output",
}


class IncidentReportRenderer:
    def __init__(self, fix_hints: Dict[str, str]) -> None:
        self._fix_hints = fix_hints

    def render(
        self,
        result: PipelineResult,
        *,
        commit_sha: Optional[str] = None,
        confidence: Optional[float] = None,
        environment: Optional[str] = None,
    ) -> str:
        if result.selected is None:
            return self._render_no_root_cause(result)

        evidence_by_id = {e["id"]: e for e in result.evidence_list}
        return self._render_selected(
            result.selected,
            evidence_by_id=evidence_by_id,
            commit_sha=commit_sha,
            confidence=confidence,
            timeline=result.timeline,
            correlation=result.correlation,
            graph=result.graph,
            fingerprint=result.fingerprint,
            remediation=result.remediation,
            hypothesis_assessment=result.hypothesis_assessment,
        )

    def _render_no_root_cause(self, result: PipelineResult) -> str:
        lines = [
            "Root Cause Analysis",
            "-" * 40,
            "",
            "No root cause identified with sufficient confidence.",
        ]
        if result.hypotheses:
            lines.append("")
            lines.append("Candidate hypotheses considered (none confident enough):")
            for h in result.hypotheses:
                lines.append(f"  - {h.label}  (score={h.score}, status={h.status})")
        else:
            lines.append("No matching evidence found in the provided inputs.")
        return "\n".join(lines)

    def _render_selected(
        self,
        selected: Hypothesis,
        *,
        evidence_by_id: Dict[str, dict],
        commit_sha: Optional[str],
        confidence: Optional[float],
        timeline: Optional[object] = None,
        correlation: Optional[object] = None,
        graph: Optional[object] = None,
        fingerprint: Optional[object] = None,
        remediation: Optional[object] = None,
        hypothesis_assessment: Optional[object] = None,
    ) -> str:
        confidence_value = confidence if confidence is not None else selected.score
        confidence_pct = round(confidence_value * 100)

        lines: List[str] = [
            "Root Cause Analysis",
            "-" * 40,
            "",
            "Root Cause:",
            f"  {self._humanize_label(selected.label)}",
            "",
            "Confidence:",
            f"  {confidence_pct}%",
            "",
            "Supporting Evidence:",
            "",
        ]

        for link in selected.links:
            evidence = evidence_by_id.get(link.evidence_id)
            marker = "[+]" if link.relation == "supports" else "[-]"
            if evidence is None:
                lines.append(f"{marker} {link.evidence_id} (evidence details unavailable)")
                continue
            source_label = _SOURCE_LABELS.get(evidence["source"], evidence["source"])
            raw_ref = evidence.get("raw_reference") or "(no raw reference)"
            lines.append(f"{marker} {source_label}")
            lines.append(f"    {raw_ref.strip()}")
            lines.append("")

        if commit_sha:
            lines += ["Related Change:", f"  commit {commit_sha}", ""]

        fix_hint = self._fix_hints.get(selected.failure_type_id)
        lines.append("Recommended Fix:")
        if fix_hint:
            lines.append(f"  {fix_hint}")
            lines.append(
                "  [Deterministic placeholder -- will be replaced by "
                "LLM-generated guidance once integrated]"
            )
        else:
            lines.append("  No fix hint configured for this failure type.")

        # ------------- حقول جديدة (لا تكسر التقرير القديم) -------------
        if remediation is not None:
            lines += self._render_remediation(remediation)
        if hypothesis_assessment is not None:
            lines += self._render_assessment(hypothesis_assessment)
        if timeline is not None:
            lines += self._render_timeline(timeline)
        if correlation is not None:
            lines += self._render_correlation(correlation)
        if graph is not None:
            lines += self._render_graph(graph)
        if fingerprint is not None:
            lines += self._render_fingerprint(fingerprint)

        return "\n".join(lines)

    def _render_remediation(self, remediation) -> List[str]:
        lines = ["", "Remediation Context:", ""]
        lines.append(f"  Action: {getattr(remediation, 'action', 'investigate_runtime_error')}")
        if getattr(remediation, "target_symbols", None):
            syms = ", ".join(remediation.target_symbols)
            lines.append(f"  Target symbols: {syms}")
        steps = list(getattr(remediation, "steps", []) or [])
        if steps:
            lines.append("  Steps:")
            for step in steps:
                lines.append(f"    - {step}")
        validation = list(getattr(remediation, "validation", []) or [])
        if validation:
            lines.append("  Validation:")
            for v in validation:
                lines.append(f"    - {v}")
        rollback = list(getattr(remediation, "rollback", []) or [])
        if rollback:
            lines.append("  Rollback:")
            for r in rollback:
                lines.append(f"    - {r}")
        return lines

    def _render_assessment(self, assessment) -> List[str]:
        lines = ["", "Candidate Hypothesis Assessment:", ""]
        selected = assessment.selected
        if selected is None:
            return lines
        lines.append(f"  Selected: {selected.label} (id={selected.id}, score={selected.score:.2f}, confidence={selected.confidence:.2f})")
        if selected.contradicting_evidence_ids:
            lines.append("  Contradicting evidence (deterministic):")
            for eid in selected.contradicting_evidence_ids:
                lines.append(f"    - {eid}")
        if selected.related_changes:
            lines.append("  Related changes (commit SHAs):")
            for c in selected.related_changes:
                lines.append(f"    - {c}")
        if selected.selection_rationale:
            lines.append(f"  Rationale: {selected.selection_rationale}")
        return lines

    def _render_timeline(self, timeline) -> List[str]:
        lines = ["", "Incident Timeline:", ""]
        if timeline.has_unknown_timestamps:
            lines.append("  (Some events have no available timestamp — clearly marked.)")
        if timeline.earliest_known_timestamp:
            lines.append(f"  Earliest known: {timeline.earliest_known_timestamp}")
        if timeline.latest_known_timestamp:
            lines.append(f"  Latest known:   {timeline.latest_known_timestamp}")
        for event in timeline.events:
            ts = event.timestamp if event.timestamp_known else "(timestamp unknown)"
            lines.append(f"  {ts}  [{event.event_type}]  {event.description}")
        return lines

    def _render_correlation(self, correlation) -> List[str]:
        lines = ["", "Evidence Correlation:", ""]
        if not correlation.edges:
            lines.append("  (No correlations detected between evidence across sources.)")
            return lines
        for edge in correlation.edges:
            lines.append(
                f"  {edge.source_observation_id} --{edge.relation}--> "
                f"{edge.target_observation_id}  (confidence={edge.confidence:.2f})"
            )
            lines.append(f"    {edge.rationale}")
        return lines

    def _render_graph(self, graph) -> List[str]:
        lines = ["", "Incident Graph:", ""]
        lines.append(f"  Nodes: {len(graph.nodes)}  Edges: {len(graph.edges)}")
        for node in graph.nodes:
            lines.append(f"    [{node.type}] {node.label}")
            if node.description:
                lines.append(f"        {node.description[:160]}")
        for edge in graph.edges:
            lines.append(f"    {edge.source_node_id} --{edge.relation}--> {edge.target_node_id}")
        return lines

    def _render_fingerprint(self, fingerprint) -> List[str]:
        lines = ["", "Incident Fingerprint:", ""]
        lines.append(f"  Failure category: {fingerprint.failure_category}")
        lines.append(f"  Failure type:     {fingerprint.failure_type}")
        lines.append(f"  Exception type:   {fingerprint.exception_type}")
        lines.append(f"  Affected service: {fingerprint.affected_service}")
        lines.append(f"  Failure stage:    {fingerprint.failure_stage}")
        lines.append(f"  Config area:      {fingerprint.configuration_area}")
        lines.append(f"  Related change:   {fingerprint.related_change_type}")
        lines.append(f"  Environment:      {fingerprint.environment}")
        lines.append(f"  Runtime type:     {fingerprint.runtime_type}")
        if fingerprint.signature_keys:
            lines.append(f"  Signature keys:   {', '.join(fingerprint.signature_keys)}")
        return lines

    @staticmethod
    def _humanize_label(label: str) -> str:
        return label.replace("_", " ").capitalize()
