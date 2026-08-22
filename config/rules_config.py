"""
config/rules_config.py
-----------------------------------------------------------------------------
نقطة تحميل وتحقق واحدة لملف rules.config.json. كل الطبقات الأخرى
(RuleEngine, ScoringEngine, EvidenceBuilder, RCARequestBuilder) تستقبل
كائن RulesConfig مُحقَّق مسبقًا، وليس dict خام — بحيث:

- أي خطأ بنيوي في rules.config.json (قسم مفقود، public_id غير صالح،
  طريقة تطبيع غير مدعومة...) يُكتشف في مكان واحد فقط، وقت التحميل، وليس
  متناثرًا بين __init__ كل طبقة على حدة.
- الطبقات الأخرى تصل للأقسام عبر attributes مكتوبة (rules_config.hypothesis_rules)
  بدل dict indexing (rules_config["hypothesis_rules"])، فأي كود يحاول
  الوصول لقسم غير موجود يفشل في وقت التطوير (AttributeError واضح)
  بدل KeyError غامض وقت التشغيل.

هذا الملف لا يحتوي على أي منطق عمل (score, confidence, severity) — فقط
تحميل + تحقق بنيوي. المنطق نفسه يبقى في RuleEngine/ScoringEngine/إلخ.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

_PUBLIC_ID_PATTERN = re.compile(r"^RC[0-9]+$")
_FAILURE_TYPE_ID_PATTERN = re.compile(r"^FT[0-9]+$")
_SUPPORTED_CONFIDENCE_METHODS = ("clamp_and_round",)
_REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "config_id",
    "classification_rules",
    "hypothesis_rules",
    "corroboration_rules",
    "decision_rules",
    "confidence_normalization",
    "severity_policy",
    "hypotheses_catalog",
    "fix_hints",
    "rca_request_limits",
)
_REQUIRED_RCA_REQUEST_LIMIT_KEYS = ("max_diff_lines", "max_log_lines", "log_context_window")


class RulesConfigError(ValueError):
    """تُرفع عند أي خلل بنيوي في rules.config.json، مكتشَف وقت التحميل."""


@dataclass(frozen=True)
class RulesConfig:
    schema_version: int
    config_id: str
    classification_rules: List[dict]
    hypothesis_rules: Dict[str, dict]
    corroboration_rules: dict
    decision_rules: dict
    confidence_normalization: dict
    severity_policy: dict
    hypotheses_catalog: Dict[str, dict]
    rca_request_limits: dict
    fix_hints: Dict[str, str]

    @classmethod
    def from_dict(cls, raw: dict) -> "RulesConfig":
        cls._validate_top_level_keys(raw)
        cls._validate_hypotheses_catalog(raw["hypotheses_catalog"])
        cls._validate_confidence_normalization(raw["confidence_normalization"])
        cls._validate_rca_request_limits(raw["rca_request_limits"])
        cls._validate_fix_hints(raw["fix_hints"])
        cls._validate_hypothesis_rules_reference_catalog(
            raw["hypothesis_rules"], raw["hypotheses_catalog"]
        )

        return cls(
            schema_version=raw["schema_version"],
            config_id=raw["config_id"],
            classification_rules=raw["classification_rules"],
            hypothesis_rules=raw["hypothesis_rules"],
            corroboration_rules=raw["corroboration_rules"],
            decision_rules=raw["decision_rules"],
            confidence_normalization=raw["confidence_normalization"],
            severity_policy=raw["severity_policy"],
            hypotheses_catalog=raw["hypotheses_catalog"],
            rca_request_limits=raw["rca_request_limits"],
            fix_hints=raw["fix_hints"],
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "RulesConfig":
        path = Path(path)
        if not path.exists():
            raise RulesConfigError(f"rules.config.json غير موجود في المسار: {path}")
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        return cls.from_dict(raw)

    # ------------------------------------------------------------------
    # تحققات بنيوية — كل واحدة مسؤولة عن قسم واحد فقط
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_top_level_keys(raw: dict) -> None:
        missing = [key for key in _REQUIRED_TOP_LEVEL_KEYS if key not in raw]
        if missing:
            raise RulesConfigError(
                f"rules.config.json ناقص الأقسام المطلوبة: {missing}"
            )

    @staticmethod
    def _validate_hypotheses_catalog(hypotheses_catalog: Dict[str, dict]) -> None:
        seen_public_ids: Dict[str, str] = {}
        for hypothesis_key, catalog_entry in hypotheses_catalog.items():
            public_id = catalog_entry.get("public_id")
            if not public_id:
                raise RulesConfigError(
                    f"hypotheses_catalog['{hypothesis_key}'] لا يحتوي على public_id."
                )
            if not _PUBLIC_ID_PATTERN.match(public_id):
                raise RulesConfigError(
                    f"public_id '{public_id}' لـ '{hypothesis_key}' لا يطابق "
                    f"pattern الـ schema ^RC[0-9]+$."
                )
            if public_id in seen_public_ids:
                raise RulesConfigError(
                    f"public_id '{public_id}' مكرر بين '{seen_public_ids[public_id]}' "
                    f"و '{hypothesis_key}'."
                )
            seen_public_ids[public_id] = hypothesis_key

            for required_field in ("failure_type_id", "label", "description"):
                if required_field not in catalog_entry:
                    raise RulesConfigError(
                        f"hypotheses_catalog['{hypothesis_key}'] ناقص الحقل '{required_field}'."
                    )

    @staticmethod
    def _validate_confidence_normalization(confidence_normalization: dict) -> None:
        method = confidence_normalization.get("method")
        if method not in _SUPPORTED_CONFIDENCE_METHODS:
            raise RulesConfigError(
                f"طريقة تطبيع غير مدعومة: '{method}'. المدعوم: {_SUPPORTED_CONFIDENCE_METHODS}"
            )
        for required_field in ("clamp", "round_to"):
            if required_field not in confidence_normalization:
                raise RulesConfigError(
                    f"confidence_normalization ناقص الحقل '{required_field}'."
                )

    @staticmethod
    def _validate_rca_request_limits(rca_request_limits: dict) -> None:
        missing = [k for k in _REQUIRED_RCA_REQUEST_LIMIT_KEYS if k not in rca_request_limits]
        if missing:
            raise RulesConfigError(f"rca_request_limits ناقص المفاتيح: {missing}")

    @staticmethod
    def _validate_fix_hints(fix_hints: Dict[str, str]) -> None:
        for key, value in fix_hints.items():
            if key == "description":
                continue  # حقل توثيقي حر، ليس failure_type_id.
            if not _FAILURE_TYPE_ID_PATTERN.match(key):
                raise RulesConfigError(
                    f"fix_hints يحتوي مفتاحًا غير صالح '{key}' — يجب أن يطابق ^FT[0-9]+$ "
                    "أو يكون 'description'."
                )
            if not isinstance(value, str) or not value.strip():
                raise RulesConfigError(f"fix_hints['{key}'] يجب أن يكون نصًا غير فارغ.")

    @staticmethod
    def _validate_hypothesis_rules_reference_catalog(
        hypothesis_rules: Dict[str, dict], hypotheses_catalog: Dict[str, dict]
    ) -> None:
        """
        تحقق تكاملي إضافي: كل hypothesis_id مُشار إليه داخل hypothesis_rules
        لازم يكون موجودًا فعليًا في hypotheses_catalog — لتفادي اكتشاف
        هذا الخطأ بالصدفة فقط عند معالجة evidence حقيقية وقت التشغيل.
        """
        for failure_type_id, rule in hypothesis_rules.items():
            for link in rule.get("links", []):
                hypothesis_id = link["hypothesis_id"]
                if hypothesis_id not in hypotheses_catalog:
                    raise RulesConfigError(
                        f"hypothesis_rules['{failure_type_id}'] يشير إلى "
                        f"hypothesis_id='{hypothesis_id}' غير موجود في hypotheses_catalog."
                    )
