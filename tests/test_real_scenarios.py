from __future__ import annotations

from pathlib import Path

from real_scenarios import (
    create_missing_dependency_scenario,
    create_missing_env_scenario,
    create_port_conflict_scenario,
    analyze_artifacts,
)
from pipeline import AnalysisPipeline

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pipeline() -> AnalysisPipeline:
    return AnalysisPipeline.from_config_files(
        PROJECT_ROOT / "rules/rules.config.json",
        PROJECT_ROOT / "taxonomy/taxonomy.yaml",
    )


def test_real_missing_env_scenario_uses_real_git_and_traceback(tmp_path):
    artifacts = create_missing_env_scenario(tmp_path)
    result = analyze_artifacts(artifacts, _pipeline(), use_llm=False)
    assert len(result["commit_sha"]) == 40
    assert Path(result["trace_or_output_file"]).read_text()
    assert result["selected_root_cause"] == "missing_environment_variable"
    assert "FT001" in result["detected_failure_types"]
    assert result["llm_status"] == "NOT VERIFIED"


def test_real_missing_dependency_scenario_executes_unavailable_import(tmp_path):
    artifacts = create_missing_dependency_scenario(tmp_path)
    output = Path(artifacts.source_file).read_text()
    result = analyze_artifacts(artifacts, _pipeline(), use_llm=False)
    assert "ModuleNotFoundError" in output
    assert result["selected_root_cause"] == "missing_dependency"
    assert "FT002" in result["detected_failure_types"]


def test_real_port_conflict_scenario_uses_two_server_processes(tmp_path):
    artifacts = create_port_conflict_scenario(tmp_path)
    output = Path(artifacts.source_file).read_text()
    result = analyze_artifacts(artifacts, _pipeline(), use_llm=False)
    assert "Address already in use" in output
    assert result["selected_root_cause"] == "port_conflict"
    assert "FT003" in result["detected_failure_types"]