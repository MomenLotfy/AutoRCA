"""
tests/test_rules_config.py
-----------------------------------------------------------------------------
يتحقق أن RulesConfig هو المكان الوحيد الذي يكتشف أخطاء بنية
rules.config.json — وليس RuleEngine أو ScoringEngine أو RCARequestBuilder
كل على حدة. أي config فاسد يجب أن يُرفض هنا، Fail Fast، قبل وصوله لأي
طبقة أخرى.
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from config.rules_config import RulesConfig, RulesConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_CONFIG_PATH = PROJECT_ROOT / "rules" / "rules.config.json"


@pytest.fixture()
def valid_raw_config() -> dict:
    with RULES_CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_valid_config_loads_successfully(valid_raw_config):
    config = RulesConfig.from_dict(valid_raw_config)
    assert config.config_id == "rules-config-v1"
    # Phase 1 — additive: RC_resource_exhaustion (FT011) added.
    assert len(config.hypotheses_catalog) == 4
    assert "RC_resource_exhaustion" in config.hypotheses_catalog
    assert config.hypotheses_catalog["RC_resource_exhaustion"]["failure_type_id"] == "FT011"


def test_from_file_loads_the_real_project_config():
    config = RulesConfig.from_file(RULES_CONFIG_PATH)
    assert config.schema_version == 1


def test_missing_top_level_section_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    del broken["decision_rules"]
    with pytest.raises(RulesConfigError, match="ناقص الأقسام"):
        RulesConfig.from_dict(broken)


def test_duplicate_public_id_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    broken["hypotheses_catalog"]["RC_missing_dependency"]["public_id"] = "RC1"
    with pytest.raises(RulesConfigError, match="مكرر"):
        RulesConfig.from_dict(broken)


def test_invalid_public_id_pattern_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    broken["hypotheses_catalog"]["RC_missing_env"]["public_id"] = "missing_env_1"
    with pytest.raises(RulesConfigError, match="pattern"):
        RulesConfig.from_dict(broken)


def test_missing_public_id_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    del broken["hypotheses_catalog"]["RC_port_conflict"]["public_id"]
    with pytest.raises(RulesConfigError, match="public_id"):
        RulesConfig.from_dict(broken)


def test_dangling_hypothesis_reference_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    broken["hypothesis_rules"]["FT001"]["links"][0]["hypothesis_id"] = "RC_ghost"
    with pytest.raises(RulesConfigError, match="غير موجود في hypotheses_catalog"):
        RulesConfig.from_dict(broken)


def test_unsupported_confidence_method_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    broken["confidence_normalization"]["method"] = "unknown_method"
    with pytest.raises(RulesConfigError, match="طريقة تطبيع غير مدعومة"):
        RulesConfig.from_dict(broken)


def test_missing_rca_request_limit_key_is_rejected(valid_raw_config):
    broken = copy.deepcopy(valid_raw_config)
    del broken["rca_request_limits"]["max_diff_lines"]
    with pytest.raises(RulesConfigError, match="rca_request_limits"):
        RulesConfig.from_dict(broken)


def test_nonexistent_file_path_is_rejected():
    with pytest.raises(RulesConfigError, match="غير موجود"):
        RulesConfig.from_file("/tmp/does-not-exist-rules.config.json")
