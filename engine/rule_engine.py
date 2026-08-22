from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from config.rules_config import RulesConfig

Evidence = Dict[str, object]


@dataclass(frozen=True)
class HypothesisLink:
    evidence_id: str
    relation: str


@dataclass(frozen=True)
class Hypothesis:
    id: str
    analysis_id: str
    schema_version: int
    failure_type_id: str
    label: str
    description: str
    links: List[HypothesisLink]
    score: float
    status: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "analysis_id": self.analysis_id,
            "schema_version": self.schema_version,
            "failure_type_id": self.failure_type_id,
            "label": self.label,
            "description": self.description,
            "links": [{"evidence_id": l.evidence_id, "relation": l.relation} for l in self.links],
            "score": self.score,
            "status": self.status,
        }


class RuleEngineError(ValueError):
    pass


class RuleEngine:
    def __init__(self, rules_config: RulesConfig) -> None:
        self._config = rules_config
        self._hypothesis_rules: Dict[str, dict] = rules_config.hypothesis_rules
        self._corroboration_rules: dict = rules_config.corroboration_rules
        self._decision_rules: dict = rules_config.decision_rules
        self._severity_policy: dict = rules_config.severity_policy
        self._hypotheses_catalog: Dict[str, dict] = rules_config.hypotheses_catalog
        self._classification_rules: List[dict] = rules_config.classification_rules
        # لا يوجد أي تحقق بنيوي هنا — RulesConfig.from_dict/from_file يضمن
        # أن public_id لكل فرضية موجود، صالح، وفريد قبل وصول الكائن لهذا
        # الملف، فلا داعي لتكرار التحقق في كل طبقة تستهلك RulesConfig.

    def build_hypotheses(self, analysis_id: str, evidence_list: List[Evidence]) -> List[Hypothesis]:
        if not evidence_list:
            return []

        links_by_hypothesis, raw_scores = self._accumulate_scores(evidence_list)

        hypotheses = [
            self._build_hypothesis(
                analysis_id=analysis_id,
                hypothesis_id=hyp_id,
                links=links_by_hypothesis[hyp_id],
                raw_score=raw_scores[hyp_id],
            )
            for hyp_id in raw_scores
        ]

        return self._apply_decision_rules(hypotheses)

    def _accumulate_scores(self, evidence_list: List[Evidence]):
        links_by_hypothesis: Dict[str, List[HypothesisLink]] = {}
        raw_scores: Dict[str, float] = {}

        # يتتبع ما إذا كانت هذه الفرضية قد استلمت بالفعل دليلها الأساسي
        # (أول دليل يصلها من أي classification_rule)، بصرف النظر عن هوية
        # تلك القاعدة. أي دليل يصل بعد ذلك للفرضية نفسها يُعامَل كدليل
        # تعزيز، بوزنه الخاص المُشتق من classification_rule_id الخاص به.
        has_received_base_evidence: Dict[str, bool] = {}

        for evidence in evidence_list:
            failure_type_id = evidence["failure_type_id"]
            classification_rule_id = evidence["classification_rule_id"]

            rule = self._hypothesis_rules.get(failure_type_id)
            if rule is None:
                continue

            for link_rule in rule["links"]:
                hypothesis_id = link_rule["hypothesis_id"]
                relation = link_rule["relation"]
                base_weight = float(link_rule["weight"])

                is_first_for_hypothesis = not has_received_base_evidence.get(hypothesis_id, False)
                has_received_base_evidence[hypothesis_id] = True

                if is_first_for_hypothesis:
                    weight = base_weight
                else:
                    weight = self._corroboration_rules["additional_evidence_weight"].get(
                        classification_rule_id, 0.0
                    )

                signed_weight = weight if relation == "supports" else -weight
                raw_scores[hypothesis_id] = raw_scores.get(hypothesis_id, 0.0) + signed_weight

                links_by_hypothesis.setdefault(hypothesis_id, []).append(
                    HypothesisLink(evidence_id=evidence["id"], relation=relation)
                )

        return links_by_hypothesis, raw_scores

    def _build_hypothesis(self, *, analysis_id, hypothesis_id, links, raw_score) -> Hypothesis:
        catalog_entry = self._hypotheses_catalog.get(hypothesis_id)
        if catalog_entry is None:
            raise RuleEngineError(
                f"hypothesis_id '{hypothesis_id}' غير موجود في hypotheses_catalog."
            )

        clamp = self._decision_rules["score_clamp"]
        clamped_score = max(clamp["min"], min(clamp["max"], raw_score))

        return Hypothesis(
            id=catalog_entry["public_id"],
            analysis_id=analysis_id,
            schema_version=1,
            failure_type_id=catalog_entry["failure_type_id"],
            label=catalog_entry["label"],
            description=catalog_entry["description"],
            links=links,
            score=round(clamped_score, 4),
            status="candidate",
        )

    def _apply_decision_rules(self, hypotheses: List[Hypothesis]) -> List[Hypothesis]:
        if not hypotheses:
            return []

        sorted_hypotheses = sorted(hypotheses, key=lambda h: h.score, reverse=True)
        top = sorted_hypotheses[0]
        second_score = sorted_hypotheses[1].score if len(sorted_hypotheses) > 1 else 0.0

        min_score = self._decision_rules["min_score_to_select"]
        min_gap = self._decision_rules["min_gap_between_top_two"]

        gap = top.score - second_score

        if top.score < min_score:
            top_status = "low_confidence"
        elif gap < min_gap and len(sorted_hypotheses) > 1:
            top_status = "low_confidence"
        else:
            top_status = "selected"

        result: List[Hypothesis] = []
        for hyp in sorted_hypotheses:
            if hyp is top:
                status = top_status
            else:
                status = "low_confidence" if top_status == "low_confidence" else "rejected"
            result.append(
                Hypothesis(
                    id=hyp.id,
                    analysis_id=hyp.analysis_id,
                    schema_version=hyp.schema_version,
                    failure_type_id=hyp.failure_type_id,
                    label=hyp.label,
                    description=hyp.description,
                    links=hyp.links,
                    score=hyp.score,
                    status=status,
                )
            )

        return result

    def resolve_severity(self, failure_type_id: str, context: Dict[str, object], default_severity: str) -> str:
        current_severity = default_severity

        for rule in self._severity_policy["escalation_rules"]:
            if self._matches_condition(rule["when"], failure_type_id, context):
                current_severity = rule["set_severity"]

        return current_severity

    @staticmethod
    def _matches_condition(when: Dict[str, object], failure_type_id: str, context: Dict[str, object]) -> bool:
        for key, expected_value in when.items():
            if key == "failure_type_id":
                if failure_type_id != expected_value:
                    return False
            elif key.startswith("context."):
                context_key = key.split(".", 1)[1]
                if context.get(context_key) != expected_value:
                    return False
        return True
