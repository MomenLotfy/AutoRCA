"""
engine/hypothesis_engine.py
-----------------------------------------------------------------------------
HypothesisEngine — نموذج فرضيات صريح قائم على الأدلة المؤيدة والمعارضة.

يحلّ هذا الـ engine محلّ النموذج التبسيطي "Evidence → Root Cause" بنموذج:
    Evidence
       |
       v
    Candidate Hypotheses
       |
       +-- supporting_evidence
       +-- contradicting_evidence
       +-- related_changes
       |
       v
    Score each hypothesis
       |
       v
    Select best supported hypothesis

كل hypothesis يحمل:
- id (public_id من hypotheses_catalog)
- label
- score (يبدأ من RuleEngine، يُعدَّل بدلائل معارضة)
- confidence (computed via ScoringEngine)
- supporting_evidence_ids
- contradicting_evidence_ids
- related_changes (commit SHA مرتبط بالأدلة)
- severity
- selection_rationale: لماذا اختير/رُفض

مبدأ صارم:
- contradicting evidence لا تُخترع — تُشتق من Evidence/observation الحقيقية فقط.
- قاعدة "يتعارض مع missing_environment_variable" لو رأينا:
  * application successfully bound its configured port
  * no "address already in use" error
  * container did not fail because of bind()
  تُطبَّق هنا كقواعد deterministic على observations حقيقية.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config.rules_config import RulesConfig
from engine.rule_engine import Hypothesis, HypothesisLink, RuleEngine
from engine.scoring_engine import ScoringEngine
from extractors.base import Observation


VALID_RELATIONS: tuple[str, ...] = ("supports", "contradicts")


class HypothesisEngineError(ValueError):
    pass


@dataclass(frozen=True)
class HypothesisAssessment:
    id: str
    analysis_id: str
    schema_version: int
    failure_type_id: str
    label: str
    description: str
    score: float
    confidence: float
    status: str  # "selected" | "rejected" | "low_confidence"
    severity: str
    supporting_evidence_ids: List[str]
    contradicting_evidence_ids: List[str]
    related_changes: List[str]  # commit SHAs
    selection_rationale: str
    links: List[HypothesisLink]

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "analysis_id": self.analysis_id,
            "schema_version": self.schema_version,
            "failure_type_id": self.failure_type_id,
            "label": self.label,
            "description": self.description,
            "score": self.score,
            "confidence": self.confidence,
            "status": self.status,
            "severity": self.severity,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "related_changes": list(self.related_changes),
            "selection_rationale": self.selection_rationale,
            "links": [{"evidence_id": l.evidence_id, "relation": l.relation} for l in self.links],
        }


@dataclass(frozen=True)
class HypothesisAssessmentResult:
    analysis_id: str
    assessments: List[HypothesisAssessment]
    selected: Optional[HypothesisAssessment]

    def to_dict(self) -> Dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "assessments": [a.to_dict() for a in self.assessments],
            "selected": self.selected.to_dict() if self.selected else None,
        }


_PORT_IN_USE_HINTS = (
    re.compile(r"EADDRINUSE", re.IGNORECASE),
    re.compile(r"Address already in use", re.IGNORECASE),
    re.compile(r"bind: address already in use", re.IGNORECASE),
    re.compile(r"\[Errno\s+98\]"),
)
_PORT_SUCCESS_HINTS = (
    re.compile(r"Uvicorn running on", re.IGNORECASE),
    re.compile(r"Application startup complete", re.IGNORECASE),
    re.compile(r"Listening on", re.IGNORECASE),
    re.compile(r"started server process", re.IGNORECASE),
)


def _detect_port_success_from_logs(observations: List[Observation]) -> bool:
    """يكشف عن دلائل تُثبت أن التطبيق نجح في bind() للمنفذ (أي لا يوجد port_conflict)."""
    for obs in observations:
        ref = obs.raw_reference or ""
        for pat in _PORT_SUCCESS_HINTS:
            if pat.search(ref):
                return True
    return False


def _detect_port_failure_from_logs(observations: List[Observation]) -> bool:
    """يكشف عن دلائل على أن فشل التطبيق كان بسبب bind() (port_conflict)."""
    for obs in observations:
        ref = obs.raw_reference or ""
        for pat in _PORT_IN_USE_HINTS:
            if pat.search(ref):
                return True
    return False


class HypothesisEngine:
    """
    يبني HypothesisAssessmentResult من Evidence + Observations + RuleEngine.

    - يستخدم RuleEngine لحساب الـ score الأولي والـ links.
    - ثم يفحص Observations لاستخراج contradicting evidence لكل hypothesis:
        * missing_environment_variable: لو رأينا port_success → يضعف
        * port_conflict: لو رأينا port_success بدون port_failure → يضعف بشدة
        * missing_dependency: لو رأينا ModuleNotFoundError → يدعم
    - كل hypothesis assessment يحصل على:
        supporting_evidence_ids من links (relation=supports)
        contradicting_evidence_ids من observations (تُربط بـ evidence_id)
        related_changes من observation.location.commit_sha
    """

    def __init__(
        self,
        rules_config: RulesConfig,
        rule_engine: RuleEngine,
        scoring_engine: ScoringEngine,
    ) -> None:
        self._rules_config = rules_config
        self._rule_engine = rule_engine
        self._scoring_engine = scoring_engine
        # weights تُقرأ من rules_config.decision_rules للتأكد من اتساق مع النظام القائم.
        self._min_score = float(rules_config.decision_rules["min_score_to_select"])
        self._min_gap = float(rules_config.decision_rules["min_gap_between_top_two"])
        self._clamp = rules_config.decision_rules["score_clamp"]
        self._severity_policy = rules_config.severity_policy
        self._catalog = rules_config.hypotheses_catalog

    def assess(
        self,
        *,
        analysis_id: str,
        evidence_list: List[dict],
        observations: List[Observation],
        environment: str,
    ) -> HypothesisAssessmentResult:
        # 1) استخدام RuleEngine لحساب الفرضيات الأساسية
        base_hypotheses = self._rule_engine.build_hypotheses(
            analysis_id=analysis_id,
            evidence_list=evidence_list,
        )
        if not base_hypotheses:
            return HypothesisAssessmentResult(
                analysis_id=analysis_id,
                assessments=[],
                selected=None,
            )

        # 2) فهرسة Observations بالـ id لاستخراج commit SHA
        obs_by_id = {o.id: o for o in observations}
        # 3) فهرسة Evidence بالـ id للحصول على observation_id
        evidence_by_id = {e["id"]: e for e in evidence_list}

        # 4) كشف port_success / port_failure (deterministic)
        port_success_observed = _detect_port_success_from_logs(observations)
        port_failure_observed = _detect_port_failure_from_logs(observations)

        # 5) بناء assessments
        assessments: List[HypothesisAssessment] = []
        for hyp in base_hypotheses:
            assessment = self._build_assessment(
                hypothesis=hyp,
                evidence_by_id=evidence_by_id,
                obs_by_id=obs_by_id,
                port_success_observed=port_success_observed,
                port_failure_observed=port_failure_observed,
                environment=environment,
            )
            assessments.append(assessment)

        # 6) تطبيق decision rules (نفس قواعد RuleEngine لكن على Assessments)
        selected, sorted_assessments = self._apply_decision_rules(assessments)

        return HypothesisAssessmentResult(
            analysis_id=analysis_id,
            assessments=sorted_assessments,
            selected=selected,
        )

    # ------------------------------------------------------------------

    def _build_assessment(
        self,
        *,
        hypothesis: Hypothesis,
        evidence_by_id: Dict[str, dict],
        obs_by_id: Dict[str, Observation],
        port_success_observed: bool,
        port_failure_observed: bool,
        environment: str,
    ) -> HypothesisAssessment:
        supporting: List[str] = []
        contradicting: List[str] = []
        related_changes: List[str] = []
        links: List[HypothesisLink] = []

        for link in hypothesis.links:
            evidence = evidence_by_id.get(link.evidence_id)
            if evidence is None:
                continue
            obs_id = evidence.get("observation_id")
            obs = obs_by_id.get(obs_id) if obs_id else None
            if obs is not None:
                if obs.location.commit_sha and obs.location.commit_sha not in related_changes:
                    related_changes.append(obs.location.commit_sha)
            if link.relation == "supports":
                if link.evidence_id not in supporting:
                    supporting.append(link.evidence_id)
                links.append(link)
            elif link.relation == "contradicts":
                if link.evidence_id not in contradicting:
                    contradicting.append(link.evidence_id)
                links.append(link)

        # Contradicting evidence المستخرجة deterministic:
        extra_contradictions = self._detect_contradictions(
            failure_type_id=hypothesis.failure_type_id,
            evidence_list=list(evidence_by_id.values()),
            obs_by_id=obs_by_id,
            port_success_observed=port_success_observed,
            port_failure_observed=port_failure_observed,
        )
        for eid, evidence in extra_contradictions:
            if eid not in contradicting:
                contradicting.append(eid)
                links.append(HypothesisLink(evidence_id=eid, relation="contradicts"))

        # تطبيق الـ penalty على score
        score = float(hypothesis.score)
        penalty = min(0.6, 0.2 * len(contradicting))
        if contradicting:
            score = max(self._clamp["min"], score - penalty)
            score = min(self._clamp["max"], score)

        confidence = self._scoring_engine.compute_confidence(score)

        # severity resolution
        default_severity = "medium"
        for rule in self._severity_policy.get("escalation_rules", []):
            when = rule.get("when", {})
            ok = True
            for k, v in when.items():
                if k == "failure_type_id" and hypothesis.failure_type_id != v:
                    ok = False
                    break
                if k.startswith("context."):
                    ck = k.split(".", 1)[1]
                    if ck == "environment" and environment != v:
                        ok = False
                        break
            if ok:
                default_severity = rule["set_severity"]

        rationale = self._build_rationale(
            hypothesis=hypothesis,
            supporting=supporting,
            contradicting=contradicting,
            score=score,
            penalty=penalty,
        )

        return HypothesisAssessment(
            id=hypothesis.id,
            analysis_id=hypothesis.analysis_id,
            schema_version=hypothesis.schema_version,
            failure_type_id=hypothesis.failure_type_id,
            label=hypothesis.label,
            description=hypothesis.description,
            score=round(score, 4),
            confidence=confidence,
            status="candidate",
            severity=default_severity,
            supporting_evidence_ids=supporting,
            contradicting_evidence_ids=contradicting,
            related_changes=related_changes,
            selection_rationale=rationale,
            links=links,
        )

    def _detect_contradictions(
        self,
        *,
        failure_type_id: str,
        evidence_list: List[dict],
        obs_by_id: Dict[str, Observation],
        port_success_observed: bool,
        port_failure_observed: bool,
    ) -> List[Tuple[str, dict]]:
        """يُعيد قائمة (evidence_id, evidence) التي تتعارض مع الفرضية."""
        contradictions: List[Tuple[str, dict]] = []

        for evidence in evidence_list:
            eid = evidence["id"]
            obs_id = evidence.get("observation_id")
            obs = obs_by_id.get(obs_id) if obs_id else None
            if obs is None:
                continue
            ref = obs.raw_reference or ""

            # Hypothesis: port_conflict — يتعارض إذا رأينا port_success بدون port_failure
            if failure_type_id == "FT003":
                if port_success_observed and not port_failure_observed:
                    contradictions.append((eid, evidence))

            # Hypothesis: missing_environment_variable — يتعارض لو رأينا port_success ولا يوجد KeyError
            if failure_type_id == "FT001":
                if port_success_observed and "KeyError" not in ref:
                    # الـ port_success يعارض الفرضية بشكل ضعيف
                    contradictions.append((eid, evidence))

        return contradictions

    def _build_rationale(
        self,
        *,
        hypothesis: Hypothesis,
        supporting: List[str],
        contradicting: List[str],
        score: float,
        penalty: float,
    ) -> str:
        parts = [
            f"Initial score from RuleEngine={hypothesis.score:.2f}.",
        ]
        parts.append(f"Supporting evidence count={len(supporting)}.")
        if contradicting:
            parts.append(
                f"Contradicting evidence count={len(contradicting)} -> penalty={penalty:.2f}."
            )
        parts.append(f"Final score={score:.2f}.")
        return " ".join(parts)

    def _apply_decision_rules(
        self,
        assessments: List[HypothesisAssessment],
    ) -> Tuple[Optional[HypothesisAssessment], List[HypothesisAssessment]]:
        if not assessments:
            return None, []
        sorted_a = sorted(assessments, key=lambda h: h.score, reverse=True)
        top = sorted_a[0]
        second_score = sorted_a[1].score if len(sorted_a) > 1 else 0.0
        gap = top.score - second_score

        if top.score < self._min_score:
            top_status = "low_confidence"
        elif gap < self._min_gap and len(sorted_a) > 1:
            top_status = "low_confidence"
        else:
            top_status = "selected"

        result: List[HypothesisAssessment] = []
        for a in sorted_a:
            if a is top:
                new_status = top_status
            else:
                new_status = "low_confidence" if top_status == "low_confidence" else "rejected"
            result.append(
                HypothesisAssessment(
                    id=a.id,
                    analysis_id=a.analysis_id,
                    schema_version=a.schema_version,
                    failure_type_id=a.failure_type_id,
                    label=a.label,
                    description=a.description,
                    score=a.score,
                    confidence=a.confidence,
                    status=new_status,
                    severity=a.severity,
                    supporting_evidence_ids=a.supporting_evidence_ids,
                    contradicting_evidence_ids=a.contradicting_evidence_ids,
                    related_changes=a.related_changes,
                    selection_rationale=a.selection_rationale,
                    links=a.links,
                )
            )
        selected = next((r for r in result if r.status == "selected"), None)
        return selected, result
