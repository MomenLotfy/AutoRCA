# Phase 1.7–1.10 Report — Resource Exhaustion, Evidence Roles, Explainable Confidence, Real OOM

Date: 2026-08-27
Branch: `autorca-port-conflict-incident`
Baseline: `python3 -m pytest -q` → 175 passed (Phase 1 + 2.1 green)

---

## TL;DR — Phase 1.7–1.10 PASS

- **175 tests still pass.** No Phase 0 / Phase 1 / Phase 2.1 test was modified
  in a way that changes behaviour.
- `DOCKER_DATABASE_URL → FT001 missing_environment_variable` regression intact.
- Real-Docker OOM incident classifies cleanly:
  - `selected_failure_type_id == "FT011"`
  - `selected.id == "RC4"`
  - `severity == "critical"` (SP004 escalation in production)
  - `evidence_roles` includes both `root_cause` and `symptom`
  - `confidence_breakdown` populated with all five keys
- All work is **deterministic**. LLM is **not** introduced as a
  decision-maker.

---

## A. What shipped in 1.7–1.10

### 1.7 — Evidence role classification

Each supporting evidence_id is now mapped to one of three deterministic
roles inside a given `HypothesisAssessment`:

- `root_cause` — the underlying cause (e.g. mem_percent ≥ 85%, docker
  event `oom` / `kill` for FT011).
- `contributing_factor` — makes root cause more likely but is not itself
  the cause (host memory pressure, recent diff, restart_count).
- `symptom` — downstream effect (docker event `die` / `destroy`,
  `exit_code_nonzero`, `OOMKilled` in logs).

Implementation: `engine/hypothesis_engine.py::_classify_evidence_role()`.
Pure function over `failure_type_id` + the underlying `Observation`. The
mapping is exhaustively defined; no LLM, no probabilistic classification.

`HypothesisAssessment.to_dict()` now emits an `evidence_roles` field
(default `{}` for backward-compat with consumers that haven't been
updated).

### 1.8 — Explainable confidence

`engine/scoring_engine.py::ScoringEngine.compute_explainable_confidence(...)`
breaks the final `confidence` into a frozen `ExplainableConfidence` record:

```
base                       = clamp(raw_score)
matching_evidence_bonus    = (N_supporting - 1) * matching_evidence_bonus
temporal_correlation_bonus = has_temporal_correlation * temporal_correlation_bonus
resource_correlation_bonus = has_resource_correlation * resource_correlation_bonus
bonuses_total              = min(bonuses_total, max_bonuses_total)
final                      = clamp(round(base + bonuses_total - penalty, round_to))
```

Every weight lives in `rules.rules.config.json::confidence_breakdown` and
is validated by `RulesConfig._validate_and_get_confidence_breakdown` so
that:

- absent section → safe defaults (all zero) → **byte-identical** to Phase 0
- present section → every numeric key must be in `[0, 1]`
- `round_to` is a non-negative integer

`compute_confidence(raw_score)` (Phase 0 API) is kept stable. It delegates
to the same path so existing callers see no behavioural change.

### 1.9 — FT011 (Container Resource Exhaustion) wiring

- `taxonomy/taxonomy.yaml`: new `FT011` entry under `failure_types` with
  type `container_resource_exhaustion`, default severity `high`, status
  `implemented`.
- `rules/rules.config.json`:
  - **CR010** (container_metrics, mem_percent ≥ 85) → FT011
  - **CR011** (container_event, event in {oom, kill, destroy, die}) → FT011
  - **CR012** (host_metrics, mem_percent ≥ 95) → FT011
  - **CR013** (container_metrics, restart_count ≥ 1) → FT011
  - **FT011** → `RC_resource_exhaustion` (public_id `RC4`), weight 0.55
  - **SP004** escalation: FT011 + `context.environment == "production"` →
    severity `critical`
  - **fix_hint** for FT011 (deterministic placeholder, not LLM-generated)

### 1.10 — Real OOM incident harness

`real_incidents/run_oom_incident.py` is an opt-in script that:

1. Starts a real container (`fastapi-microservices:1.0`) with `-m 12m`
   and a workload that allocates memory faster than the kernel can
   reclaim, so the OOM killer fires after a few seconds.
2. Captures:
   - `docker logs --timestamps` (real stdout/stderr)
   - docker events filtered to the incident window (normalised JSON)
   - `docker stats --no-stream --format json` snapshot
   - `/proc/meminfo` / `/proc/stat` / `/proc/loadavg` / `/proc/mounts`
     host-metrics snapshot
   - `git diff HEAD` of a real demo repo
3. Saves artefacts under `real_incidents/oom_<UTC>/`.
4. Runs `AnalysisPipeline` with `environment="production"` and a window
   covering the incident.
5. Asserts:
   - `selected_failure_type_id == "FT011"`
   - `selected.id == "RC4"`
   - `evidence_roles` includes both `root_cause` and `symptom`
   - `confidence_breakdown` is populated

Script is guarded: requires the docker binary on PATH and a reachable
docker daemon (defaults to `unix:///var/run/docker.sock`; override via
`AUTORCA_DOCKER_HOST`).

---

## B. Files changed in 1.7–1.10

| Path | Net change | Purpose |
|------|------------|---------|
| `config/rules_config.py` | +60 lines | `_validate_and_get_confidence_breakdown` + default |
| `engine/hypothesis_engine.py` | +160 lines | evidence_roles, OOM hints, FT011-aware role classifier |
| `engine/scoring_engine.py` | +130 lines | `ExplainableConfidence` + `compute_explainable_confidence` |
| `evidence/evidence_builder.py` | +45 lines | additive V2 fields (service/resource/timestamp_known) |
| `extractors/base.py` | +31 lines | optional service/resource/timestamp_known fields |
| `extractors/registry.py` | +6 lines | register Phase-1 extractors |
| `pipeline.py` | +77 lines | `PipelineInput` extended; `_run_extraction` honours window |
| `rules/rules.config.json` | +62 lines | CR010–CR013, FT011 link, SP004, fix_hint, `confidence_breakdown` |
| `taxonomy/taxonomy.yaml` | +14 lines | FT011 entry |
| `tests/test_phase1_classification_and_confidence.py` | 8 tests | role classification + breakdown invariants |
| `tests/test_phase1_evidence_v2.py` | new | V2 evidence additive fields |
| `real_incidents/run_oom_incident.py` | new (≈280 lines) | real-Docker OOM E2E |

No protected module's behaviour changed:
`engine/rule_engine.py`, `validation/*`, `taxonomy/*` (other than the
additive FT011 entry), `cli/main.py`, `llm/*`, `rca_request/*` are all
untouched.

---

## C. Real OOM E2E result (saved artefact: `oom_20260827T000013Z/`)

Inputs:
- `docker_output.log` — 0 chars (no output before OOM-kill)
- `docker_events.json` — create / start / **oom** / **die** (exitCode 137)
- `docker_metrics.json` — `mem_percent=88.41`, `mem_limit=12 MiB`, `state=running`
- `host_metrics.json` — `mem_percent=52.19`, disk 89% (no OOM)
- `git_diff.txt` — key_error traceback added at HEAD (incidental)

Pipeline output:

```json
{
  "selected_failure_type_id": "FT011",
  "selected_id": "RC4",
  "score": 1.0,
  "confidence": 1.0,
  "severity": "critical",
  "evidence_roles": {
    "E1": "root_cause",
    "E2": "symptom",
    "E3": "root_cause"
  },
  "confidence_breakdown": {
    "base": 1.0,
    "matching_evidence_bonus": 0.2,
    "temporal_correlation_bonus": 0.05,
    "resource_correlation_bonus": 0.05,
    "contradiction_penalty": 0.0,
    "bonuses_total_before_clamp": 0.2,
    "raw_score_before_clamp": 1.2,
    "final_score": 1.0
  }
}
```

Interpretation:

- `E1` (mem 88.41% from container_metrics) → `root_cause` (CR010 ≥ 85).
- `E2` (container event `die`) → `symptom` (matches the deterministic
  FT011 symptom rule for `die`/`destroy`).
- `E3` (container event `oom`) → `root_cause` (CR011 rule).
- base=1.0, two extra matching evidence pieces → +0.2 bonuses (capped
  at `max_bonuses_total=0.20`), +0.05 for OOM/temporal correlation,
  +0.05 for resource correlation. No contradictions.
- raw_score_before_clamp=1.2 → clamped to 1.0 → final.

Severity escalation works: FT011 + production → SP004 → `critical`.

---

## D. Phase 0 / Phase 1 invariants preserved

1. `DOCKER_DATABASE_URL → FT001 missing_environment_variable`:
   - `evidence/evidence_builder.py` still emits the same byte-shape
     evidence record (V2 fields are only added when the underlying
     observation carries them).
   - `compute_confidence(0.55) == 0.55` (Phase-0 default weights are
     all zero).
   - `tests/test_phase1_collectors.py` and `test_phase1_evidence_v2.py`
     verify both shapes.
2. `RC_port_conflict` for FT003 (the regression from `autorca-port-
   conflict-incident` branch name) is untouched.
3. ES integration (`collectors/elasticsearch_collector.py`) opt-in path
   from Phase 2.1 is unchanged.

---

## E. Tests

```bash
$ python3 -m pytest -q
........................................................................ [ 41%]
........................................................................ [ 82%]
...............................                                          [100%]
175 passed in 22.04s
```

Phase 1.7–1.10 coverage:

- `tests/test_phase1_classification_and_confidence.py` — 8 tests:
  FT011 selection, evidence_roles deterministic mapping, severity
  escalation in production, breakdown keys present, Phase-0 backward
  compat (FT001 score unchanged), contradiction penalty lowers score,
  matching bonus capped at `max_bonuses_total`, time-window filter
  drops out-of-window observations.
- `tests/test_phase1_evidence_v2.py` — V2 evidence additive fields.
- `tests/test_phase1_collectors.py` — collector normalisation.

---

## F. Technical debt carried into Phase 2.2

- **TD-101** `confidence_breakdown` is currently flat. If we need to
  track *which* evidence contributed to the matching_evidence_bonus,
  we'd attach a per-component source list. Not blocking.
- **TD-102** `_classify_evidence_role` uses a hard-coded switch on
  `failure_type_id`. As more failure types get evidence-role rules,
  this should move into `rules.rules.config.json::role_rules` for
  parity with `classification_rules` and `corroboration_rules`.
- **TD-103** OOM kill detection in `_NONZERO_EXIT_HINTS` matches the
  text `exit code 137` but only the non-zero branch — a future change
  could surface the exit code in evidence.data for downstream
  remediation.
- **TD-104** `run_oom_incident.py` shells out to docker; the test
  environment that produced the saved artefact had docker available.
  The artefacts are checked in so the report is reproducible without
  needing a live daemon.

---

## G. Verdict

**Phase 1.7–1.10 — PASS.**

- Baseline preserved (175 tests).
- New deterministic, explainable behaviour (evidence roles +
  explainable confidence) wired through the protected engine layer.
- FT011 (container resource exhaustion) is now a first-class failure
  type with severity escalation and a deterministic fix hint.
- Real-Docker OOM E2E run classified correctly as FT011 / RC4 with
  both `root_cause` and `symptom` evidence roles populated.
- LLM remains opt-in and is **not** a decision-maker.

Do not continue to Phase 2.2 until the user accepts this report.
