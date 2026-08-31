"""
engine/scoring_engine.py
-----------------------------------------------------------------------------
ScoringEngine — يطبّق قواعد تطبيع confidence من rules.config.json.

السلوك الكلاسيكي (Phase 0): clamp(round(raw_score)).

Phase 1.8 — explainable confidence:
  compute_explainable_confidence(...) يكسر القيمة إلى:
    base + matching_evidence_bonus + temporal_correlation_bonus
      + resource_correlation_bonus - contradiction_penalty
  مع كسر مكوّنات deterministic مُتحقَّق منها في RulesConfig.from_dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from config.rules_config import RulesConfig


@dataclass(frozen=True)
class ExplainableConfidence:
    """Phase 1.8 — تفصيل deterministic لقيمة confidence.

    كل مكوّن له وزن ثابت في rules.config.json (`confidence_breakdown`).
    الـ breakdown كائن ثابت لا يحتوي على أي معلومة خارجة عن score
    الأصلي + عدد الأدلة + قائمة التضاربات — أي يمكن إعادة حسابه من
    الـ inputs وحدها (reproducibility).
    """

    base: float
    matching_evidence_bonus: float
    temporal_correlation_bonus: float
    resource_correlation_bonus: float
    contradiction_penalty: float
    bonuses_total_before_clamp: float
    raw_score_before_clamp: float
    final_score: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "base": round(self.base, 4),
            "matching_evidence_bonus": round(self.matching_evidence_bonus, 4),
            "temporal_correlation_bonus": round(self.temporal_correlation_bonus, 4),
            "resource_correlation_bonus": round(self.resource_correlation_bonus, 4),
            "contradiction_penalty": round(self.contradiction_penalty, 4),
            "bonuses_total_before_clamp": round(self.bonuses_total_before_clamp, 4),
            "raw_score_before_clamp": round(self.raw_score_before_clamp, 4),
            "final_score": round(self.final_score, 4),
        }


class ScoringEngine:
    def __init__(self, rules_config: RulesConfig) -> None:
        # لا يوجد تحقق هنا — RulesConfig.from_dict/from_file يضمن مسبقًا
        # أن confidence_normalization.method مدعوم وأن clamp/round_to موجودان.
        normalization = rules_config.confidence_normalization
        self._clamp = normalization["clamp"]
        self._round_to = normalization["round_to"]

        # Phase 1.8 — explainable confidence weights.
        self._breakdown = rules_config.confidence_breakdown
        self._matching_bonus = float(self._breakdown["matching_evidence_bonus"])
        self._temporal_bonus = float(self._breakdown["temporal_correlation_bonus"])
        self._resource_bonus = float(self._breakdown["resource_correlation_bonus"])
        self._contradiction_penalty_unit = float(self._breakdown["contradiction_penalty"])
        self._max_bonuses_total = float(self._breakdown["max_bonuses_total"])
        self._breakdown_round_to = int(self._breakdown["round_to"])

    def compute_confidence(self, raw_score: float) -> float:
        """Phase 0 API — kept stable.

        Phase 1 behaviour: with all bonuses at 0 (Phase-0 rules.config.json
        default), this reduces to the same clamp+round used before, so
        FT001 / FT002 / FT003 numbers are byte-identical.
        """
        clamped = max(self._clamp["min"], min(self._clamp["max"], raw_score))
        return round(clamped, self._round_to)

    # ------------------------------------------------------------------
    # Phase 1.8 — explainable confidence
    # ------------------------------------------------------------------

    def compute_explainable_confidence(
        self,
        raw_score: float,
        *,
        matching_evidence_count: int = 0,
        has_temporal_correlation: bool = False,
        has_resource_correlation: bool = False,
        contradiction_count: int = 0,
    ) -> ExplainableConfidence:
        """حساب confidence مع breakdown كامل.

        صيغة الـ base score:
          base = clamp(raw_score)

        صيغة الـ bonuses (كلها deterministic، من rules.config.json):
          matching_evidence_bonus    = max(0, matching_evidence_count - 1) * matching_evidence_bonus
          temporal_correlation_bonus = has_temporal_correlation * temporal_correlation_bonus
          resource_correlation_bonus = has_resource_correlation * resource_correlation_bonus
          bonuses_total = sum(bonuses) (محدد بـ max_bonuses_total كحد أعلى)

        final = clamp(base + bonuses_total - contradiction_penalty)
        """
        base = float(max(self._clamp["min"], min(self._clamp["max"], raw_score)))

        extra_matching = max(0, int(matching_evidence_count) - 1)
        matching_bonus_total = extra_matching * self._matching_bonus
        temporal_bonus_total = self._temporal_bonus if has_temporal_correlation else 0.0
        resource_bonus_total = self._resource_bonus if has_resource_correlation else 0.0

        bonuses_total = matching_bonus_total + temporal_bonus_total + resource_bonus_total
        bonuses_total = min(bonuses_total, self._max_bonuses_total)
        bonuses_total = max(0.0, bonuses_total)

        contradiction_pen_total = max(0, int(contradiction_count)) * self._contradiction_penalty_unit

        raw_score_before_clamp = base + bonuses_total - contradiction_pen_total
        clamped = max(self._clamp["min"], min(self._clamp["max"], raw_score_before_clamp))
        final = round(clamped, self._breakdown_round_to)

        return ExplainableConfidence(
            base=round(base, self._breakdown_round_to),
            matching_evidence_bonus=round(matching_bonus_total, self._breakdown_round_to),
            temporal_correlation_bonus=round(temporal_bonus_total, self._breakdown_round_to),
            resource_correlation_bonus=round(resource_bonus_total, self._breakdown_round_to),
            contradiction_penalty=round(contradiction_pen_total, self._breakdown_round_to),
            bonuses_total_before_clamp=round(bonuses_total, self._breakdown_round_to),
            raw_score_before_clamp=round(raw_score_before_clamp, self._breakdown_round_to),
            final_score=final,
        )
