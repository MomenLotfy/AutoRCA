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
    ) -> str:
        if result.selected is None:
            return self._render_no_root_cause(result)

        evidence_by_id = {e["id"]: e for e in result.evidence_list}
        return self._render_selected(
            result.selected,
            evidence_by_id=evidence_by_id,
            commit_sha=commit_sha,
            confidence=confidence,
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

        return "\n".join(lines)

    @staticmethod
    def _humanize_label(label: str) -> str:
        return label.replace("_", " ").capitalize()
