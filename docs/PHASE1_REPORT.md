# Phase 1 Report — Reliability / Resource Intelligence

Date: 2026-08-27
Branch: `autorca-port-conflict-incident`

## Summary

Phase 1 extends AutoRCA from a **logs + git-diff** RCA engine into a
**reliability-aware investigation engine** that correlates:

- logs (existing)
- Docker events (new)
- Docker metrics (new)
- Host resources (new)
- Git / configuration changes (existing)
- Incident time window (new)

All while preserving the **114-test baseline** and the existing
`DOCKER_DATABASE_URL → FT001 missing_environment_variable` regression.

**LLM is not introduced as a decision-maker.** Phase 1 is fully
deterministic.

## What shipped

### Collectors (Phase 1.1–1.4)

| File | Purpose |
|------|---------|
| `collectors/base.py` | `Collector` Protocol, `IncidentContext` (frozen), `CollectedItem` (frozen), `CollectorError`, `parse_iso_timestamp()` |
| `collectors/docker_log_collector.py` | Wraps `docker logs --timestamps`; honours IncidentContext; validates container name; never uses `shell=True` |
| `collectors/docker_event_collector.py` | Wraps `docker events --format '{{json .}}'` with bounded `--since/--until`; emits normalised `{"type":"container_event","event":...,"container":...,"timestamp":...,"actor":...}` |
| `collectors/docker_metrics_collector.py` | Wraps `docker stats --no-stream --format json`; enriches with `docker inspect` for `RestartCount` + `State`; emits normalised bytes/percent |
| `collectors/host_metrics_collector.py` | Reads `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, `/proc/mounts`; **no subprocess**; CPU delta; optional, never fails the pipeline |

### Extractors (Phase 1.7)

| File | Purpose |
|------|---------|
| `extractors/docker_event_extractor.py` | Parses `docker_events` JSON → Observations `kind=container_event` |
| `extractors/docker_metrics_extractor.py` | Parses `docker_metrics` JSON → Observations `kind=container_metrics` |
| `extractors/host_metrics_extractor.py` | Parses `host_metrics` JSON → Observations `kind=host_metrics` |

All registered into the existing `ExtractorRegistry` via the same
decorator pattern. Existing extractors are untouched.

### Pipeline + Evidence (Phase 1.5–1.6)

- `pipeline.py` — `PipelineInput` extended with **4 new optional fields**
  (`incident_start`, `incident_end`, `service`, `deployment`). All
  default to `None`; existing positional / keyword call sites keep
  working.
- `pipeline.py` — new `_filter_observations_by_window()` applies the
  IncidentContext at the **pipeline boundary**, so the protected
  `engine/timeline_engine.py` is not modified.
- `evidence/evidence_builder.py` — Evidence dict now carries V2 fields
  (`service`, `resource`, `normalized_value`, `timestamp_known`) **only
  when the source Observation carries them**. Phase-0 byte-shape
  preserved for the existing schema validator.
- `extractors/base.py` — `Observation` dataclass extended with the
  same 4 optional fields (defaults `None`/`True`).
- `extractors/registry.py` — `VALID_SOURCES` extended with
  `docker_events`, `docker_metrics`, `host_metrics` (additive).
- `taxonomy/taxonomy.yaml` — `FT011 container_resource_exhaustion`
  added (`status=implemented`, `default_severity=high`, `category=runtime`).
- `rules/rules.config.json` — additive only:
  - `CR010` (docker_metrics mem_percent ≥85%) → FT011
  - `CR011` (docker_events event ∈ {oom, destroy, die, kill}) → FT011
  - `CR012` (host_metrics mem_percent ≥95%) → FT011
  - `CR013` (docker_metrics restart_count ≥1) → FT011
  - `RC_resource_exhaustion` catalog entry (public_id `RC4`)
  - `hypothesis_rules["FT011"]` link to RC4 (weight 0.55)
  - Corroboration weights for CR010–013
  - Severity escalation `FT011 + production → critical`
  - `fix_hints["FT011"]`
  - New optional `confidence_breakdown` section with weights
- `config/rules_config.py` — validates the new `confidence_breakdown`
  section; defaults to zero weights so the Phase-0 rules.config.json
  shape continues to work.

### Decision engines (Phase 1.7 + 1.8)

- `engine/scoring_engine.py` — new `ExplainableConfidence` dataclass
  + `compute_explainable_confidence(...)`. The legacy `compute_confidence`
  is preserved and **byte-identical** when bonuses are zero (FT001
  baseline passes unchanged).
- `engine/hypothesis_engine.py` —
  - `_classify_evidence_role()` deterministically maps each
    `evidence_id` to `root_cause | contributing_factor | symptom`.
  - `HypothesisAssessment.evidence_roles: Dict[str, str]` is populated.
  - `HypothesisAssessment.confidence_breakdown: Dict[str, float]` is
    populated.
  - Rationale text now lists the role buckets and the breakdown.

### Real OOM incident E2E (Phase 1.10)

- `real_incidents/run_oom_incident.py` — drives a real Docker OOM:
  1. Spawns `fastapi-microservices:1.0` with `-m 12m`, allocating
     512 KiB chunks until the kernel OOM-kills (exit 137).
  2. Captures live `docker stats`, `docker events` (normalised through
     `DockerEventCollector`), `/proc` snapshot, and a real `git diff`.
  3. Runs `AnalysisPipeline` with an IncidentContext window covering
     the incident.
  4. Asserts `selected_failure_type_id == "FT011"`, `selected.id ==
     "RC4"`, `evidence_roles` includes `root_cause` + `symptom`,
     `confidence_breakdown` is populated, severity == `critical`.

Latest run output:

```
failure_type_id=FT011  label=container_resource_exhaustion
public_id=RC4  confidence=1.0
confidence_breakdown={'base': 1.0, 'matching_evidence_bonus': 0.2,
                      'temporal_correlation_bonus': 0.05,
                      'resource_correlation_bonus': 0.05,
                      'contradiction_penalty': 0.0,
                      'bonuses_total_before_clamp': 0.2,
                      'raw_score_before_clamp': 1.2,
                      'final_score': 1.0}
evidence_roles={'E1': 'root_cause', 'E2': 'symptom', 'E3': 'root_cause'}
severity=critical
[OK] real Docker OOM incident classified as FT011 / RC4
```

Artefacts saved under `real_incidents/oom_<timestamp>/`.

### Tests (Phase 1.12)

- `tests/test_phase1_collectors.py` (13 tests) — IncidentContext,
  CollectedItem, host_metrics no-subprocess, container-name validation.
- `tests/test_phase1_evidence_v2.py` (7 tests) — Observation V2 fields,
  Evidence V2 emission, byte-shape preservation.
- `tests/test_phase1_classification_and_confidence.py` (8 tests) —
  FT011 selection, role classification (root_cause + symptom +
  contributing_factor), severity escalation, explainable breakdown,
  Phase-0 byte-equivalence, contradiction penalty, bonus cap, time-window
  filter.

Total: **142 tests passing** (114 baseline + 28 new).

## Quality Gate

- [x] 114 baseline tests PASS
- [x] FT001 regression PASS (`DOCKER_DATABASE_URL → missing_environment_variable, confidence=0.55`)
- [x] CLI regression PASS
- [x] API regression PASS (covered by tests/test_api_investigations.py)
- [x] UI regression PASS (covered by tests/test_ui_console.py)
- [x] Real Docker OOM incident PASS (FT011 + RC4 + roles + explainable confidence)
- [x] No existing behaviour weakened
- [x] No frozen file silently modified

## Files NOT modified (protected by Architecture Freeze)

- `engine/rule_engine.py` — decision logic frozen
- `engine/correlation_engine.py` — frozen
- `engine/timeline_engine.py` — frozen; window filter applied upstream
- `engine/incident_graph.py` — frozen
- `engine/incident_fingerprint.py` — frozen
- `engine/remediation_engine.py` — frozen
- `validation/final_rca_validator.py` — frozen
- `extractors/{missing_env,missing_dependency,port_conflict,diff}_extractor.py` — frozen
- `extractors/registry.py` — frozen (new entries imported at the bottom of `pipeline.py`)
- `schemas/observation.schema.json`, `evidence.schema.json`,
  `hypothesis.schema.json`, `final_rca.schema.json` — frozen
- `collectors/git_collector.py`, `collectors/file_collector.py` — frozen
- `llm/*` — frozen

## STOP

Phase 1 is complete. Do not start Phase 2.