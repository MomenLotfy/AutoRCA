from __future__ import annotations

from config.rules_config import RulesConfig


class ScoringEngine:
    def __init__(self, rules_config: RulesConfig) -> None:
        # لا يوجد تحقق هنا — RulesConfig.from_dict/from_file يضمن مسبقًا
        # أن confidence_normalization.method مدعوم وأن clamp/round_to موجودان.
        normalization = rules_config.confidence_normalization
        self._clamp = normalization["clamp"]
        self._round_to = normalization["round_to"]

    def compute_confidence(self, raw_score: float) -> float:
        clamped = max(self._clamp["min"], min(self._clamp["max"], raw_score))
        return round(clamped, self._round_to)
