# AutoRCA — Architecture Freeze / Engineering Contract

> **Status:** FROZEN — pre-implementation audit.
> **Date:** 2026-08-26
> **Audience:** Principal Architect, Reliability Engineer, future Phase 1 implementer.
> **Authority of this document:** Until a future explicit unfreeze, this
> document is the source of truth for what AutoRCA is, what it is not, and
> what may be changed.

---

## 0. Baseline Verification

```
python3 -m pytest -q
→ 114 passed in 18.43s
```

Baseline confirmed. All invariants below are defined against this 114-test
green baseline. If a future change breaks the baseline, STOP and audit
before continuing.

---

## 1. Architecture Map (As-Discovered)

The diagram in the prompt is correct in shape but understates several
real artefacts. The actual conceptual flow is:

```
                                ┌──────────────────────────────────┐
                                │  Incident Sources (raw)          │
                                │  traceback | git_diff | ci_log   │
                                │  docker_output | test_output     │
                                └─────────────────┬────────────────┘
                                                  │
                                                  ▼
                          ┌────────────────────────────────────────┐
                          │ Collectors (subprocess / fs)           │
                          │  GitCollector     FileCollector        │
                          │  (more to come — GitHub, K8s, …)       │
                          └─────────────────┬──────────────────────┘
                                            │ {source_name: raw_text}
                                            ▼
                          ┌────────────────────────────────────────┐
                          │ AnalysisPipeline.run()  (pipeline.py)  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ _run_extraction                  │  │
                          │  │   ExtractorRegistry (by source) │  │
                          │  │   per-source: List[Extractor]    │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ EvidenceBuilder                  │  │
                          │  │   classification_rules (CR00x)   │  │
                          │  │   → List[Evidence]               │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ RuleEngine                       │  │
                          │  │   decision + score + severity    │  │
                          │  │   → List[Hypothesis]             │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ TimelineEngine                   │  │
                          │  │   IncidentTimeline               │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ CorrelationEngine                │  │
                          │  │   CorrelationGraph (edges)       │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ IncidentGraphBuilder             │  │
                          │  │   IncidentGraph (nodes + edges)  │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ IncidentFingerprintBuilder       │  │
                          │  │   IncidentFingerprint            │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ RemediationEngine                │  │
                          │  │   RemediationContext (per FT00x) │  │
                          │  └──────────────────────────────────┘  │
                          │  ┌──────────────────────────────────┐  │
                          │  │ HypothesisEngine                 │  │
                          │  │   contradiction + assessment     │  │
                          │  │   → HypothesisAssessmentResult   │  │
                          │  └──────────────────────────────────┘  │
                          └────────────────────┬───────────────────┘
                                               │
                                               ▼
                          ┌────────────────────────────────────────┐
                          │ PipelineResult (frozen dataclass)      │
                          │   observations, evidence_list,         │
                          │   hypotheses, timeline, correlation,   │
                          │   graph, fingerprint, remediation,     │
                          │   hypothesis_assessment                 │
                          └────────────────────┬───────────────────┘
                                               │
              ┌────────────────────────────────┼─────────────────────────────┐
              │                                │                             │
              ▼                                ▼                             ▼
   ┌──────────────────────┐        ┌──────────────────────┐       ┌──────────────────────┐
   │  IncidentReport      │        │  RCARequestBuilder   │       │  Investigation UI    │
   │  Renderer (CLI /     │        │  → RCARequest        │       │  via Investigation   │
   │  terminal)           │        │  (the LLM boundary)  │       │  Service + API v1    │
   └──────────────────────┘        └──────────┬───────────┘       └──────────────────────┘
                                               │
                                               ▼
                                  ┌──────────────────────────┐
                                  │ LLMClient (Protocol)     │
                                  │  OpenAI-compatible impl  │
                                  │  ↳ FinalRCA JSON         │
                                  └──────────┬───────────────┘
                                             │
                                             ▼
                                  ┌──────────────────────────┐
                                  │ FinalRCAValidator        │
                                  │ 6 invariants, schema v1  │
                                  │ (decision boundary)      │
                                  └──────────────────────────┘
```

Notes on the diagram vs. the prompt's diagram:

- The prompt listed "Hypotheses" between Evidence and "Deterministic RCA".
  The real implementation has two hypothesis layers:
  `RuleEngine.Hypothesis` (basic, used in CLI & API summary) and
  `HypothesisEngine.HypothesisAssessment` (richer, with contradicting
  evidence and rationale). They are **not duplicates** — the rich one is
  additive on top of the basic one. Both are deterministic.
- The "Fingerprint" stage is more than a label — it carries
  signature_keys (env vars, modules) for matching.
- The Remediation stage is a structured object, not a single hint string.

---

## 2. Data-Flow Map

### 2.1 Inputs (immutable)
- **PipelineInput** (`pipeline.py`): `analysis_id`, `sources: Dict[str, str]`,
  `environment`, `commit_sha`.
- **Collector output** is also `Dict[str, str]` keyed by source name. Same
  shape as `PipelineInput.sources`. Collectors are plug-compatible.

### 2.2 Transformations (each step is deterministic, pure given its inputs)

| Step | Module | Input | Output | Determinism guarantee |
|---|---|---|---|---|
| Extraction | `extractors/*` via registry | `{source: text}` + `ExtractionContext` | `List[Observation]` | regex-based, idempotent |
| Classification | `evidence/evidence_builder.py` | `Observation[]` | `List[Evidence]` (dicts) | rule-engine + re.match |
| Scoring | `engine/rule_engine.py` | `Evidence[]` | `List[Hypothesis]` | numeric, clamp `0..1` |
| Decision | `engine/rule_engine._apply_decision_rules` | `Hypothesis[]` | `Hypothesis[]` w/ `status` | `min_score_to_select`, `min_gap` |
| Timeline | `engine/timeline_engine.py` | `Observation[]` + `started_at` | `IncidentTimeline` | ISO timestamps or `None` |
| Correlation | `engine/correlation_engine.py` | `Observation[]` | `CorrelationGraph` | regex symbol match |
| Causal graph | `engine/incident_graph.py` | correlation + observations + selected_h | `IncidentGraph` | typed nodes/edges |
| Fingerprint | `engine/incident_fingerprint.py` | observations + selected_h | `IncidentFingerprint` | table lookups |
| Remediation | `engine/remediation_engine.py` | selected_h + evidence | `RemediationContext` | per-FT00x builders |
| Contradiction + Assessment | `engine/hypothesis_engine.py` | hypothesis + observations + evidence | `HypothesisAssessmentResult` | deterministic port-success/port-failure regex |
| Confidence | `engine/scoring_engine.compute_confidence` | raw score | clamped rounded float | `clamp_and_round` |
| Severity | `engine/rule_engine.resolve_severity` | failure_type_id + ctx | severity string | last-match wins in `severity_policy` |
| LLM payload | `rca_request/rca_request_builder.py` | selected + evidence + limits | `RCARequest` (schema v1) | truncation + summarisation |
| LLM boundary | `validation/final_rca_validator.py` | RCARequest + FinalRCA | `ValidationResult` | 6 invariants + jsonschema |

### 2.3 Outputs
- `PipelineResult` (frozen, additive fields).
- `RCARequest` (JSON-serialisable, schema-validated before send).
- `FinalRCA` (LLM-emitted, validated against deterministic decision).
- HTTP responses: `/api/analyze` (legacy) + `/api/v1/investigations*` (current).

---

## 3. Component Map

### 3.1 Core domain objects
- `Observation` (`extractors/base.py`): the **only** output of collectors,
  going through extractors. Schema v1, kind enum, source enum.
- `Location` (`extractors/base.py`): `file`, `line`, `commit_sha`, `step_name`.
- `Evidence` (dict, schema-validated): classified observation.
- `Hypothesis` (`engine/rule_engine.py`): candidate root cause w/ score, links.
- `HypothesisAssessment` (`engine/hypothesis_engine.py`): extended hypothesis
  with contradicting evidence, related changes, selection rationale.
- `IncidentTimeline` / `TimelineEvent` (`engine/timeline_engine.py`).
- `CorrelationGraph` / `CorrelationEdge` (`engine/correlation_engine.py`).
- `IncidentGraph` / `GraphNode` / `GraphEdge` (`engine/incident_graph.py`).
- `IncidentFingerprint` (`engine/incident_fingerprint.py`).
- `RemediationContext` (`engine/remediation_engine.py`).
- `RCARequest` (`rca_request/rca_request_builder.py`): the LLM boundary payload.
- `FinalRCA` (dict, schema-validated by jsonschema): LLM output.
- `Investigation` (`api/investigation_service.py`): in-memory record of a
  completed run; not persisted across process restarts.

### 3.2 Orchestrators
- `AnalysisPipeline` (`pipeline.py`): the **only** deterministic orchestrator.
  Owns `RuleEngine`, `ScoringEngine`, `EvidenceBuilder`, `TimelineEngine`,
  `CorrelationEngine`, `IncidentGraphBuilder`, `IncidentFingerprintBuilder`,
  `RemediationEngine`, `HypothesisEngine`.
- `InvestigationService` (`api/investigation_service.py`): HTTP-side
  orchestrator. Validates requests, calls collectors, calls pipeline,
  serialises result.
- `AutoRCAHandler` (`web_app.py`): stdlib HTTP request handler.

### 3.3 Config / validation surface
- `RulesConfig` (`config/rules_config.py`): single source of structural
  validation for `rules/rules.config.json`.
- `taxonomy/taxonomy.yaml`: failure-type definitions, default severities,
  examples.
- `schemas/*.json`: jsonschema Draft-07 contracts.
- `validation/final_rca_validator.py`: 6 invariants.

### 3.4 LLM layer (strictly downstream)
- `llm/client.py`: Protocol `LLMClient`.
- `llm/openai_client.py`: OpenAI-compatible HTTP implementation.
- `llm/service.py`: `LLMAnalysisService.generate_validated` — calls client
  then validator. Raises `LLMClientError` or `FinalRCAValidationError`.
- `llm/prompts.py`: system prompt is **hard-coded** to forbid LLM from
  changing decisions.

### 3.5 Reporting
- `reporting/incident_report_renderer.py`: deterministic text report from
  `PipelineResult`. Uses `fix_hints` placeholder; LLM not called.
- `web/static/`: Investigation UI; pure presentation, no decision logic.

---

## 4. Dependency Map (imports only, no transitive)

```
pipeline.py
  ├─ config.rules_config
  ├─ engine.correlation_engine
  ├─ engine.hypothesis_engine
  ├─ engine.incident_fingerprint
  ├─ engine.incident_graph
  ├─ engine.remediation_engine
  ├─ engine.rule_engine
  ├─ engine.scoring_engine
  ├─ engine.timeline_engine
  ├─ evidence.evidence_builder
  ├─ extractors.base
  ├─ extractors.registry
  └─ extractors.{missing_env,missing_dependency,port_conflict,diff}_extractor

api.investigation_service
  ├─ collectors.file_collector
  ├─ collectors.git_collector
  ├─ pipeline
  ├─ api.security
  └─ api.serializers

api.serializers
  ├─ api.security.mask_secret
  └─ engine.{correlation,incident_fingerprint,incident_graph,remediation,timeline,hypothesis_engine}

cli.main
  ├─ collectors.{file,git}_collector
  ├─ pipeline
  ├─ rca_request.rca_request_builder
  ├─ reporting.incident_report_renderer
  └─ llm.{LLMAnalysisService, LLMClientError, OpenAICompatibleLLMClient}

web_app.py
  ├─ collectors.{file,git}_collector
  ├─ llm (LLM boundary)
  ├─ pipeline
  ├─ rca_request.rca_request_builder
  └─ api.investigation_service

engine.* → extractors.base, config.rules_config, engine.{rule,scoring}_engine
extractors.* → extractors.base + extractors.registry
evidence.evidence_builder → extractors.base + config.rules_config
llm.* → rca_request.rca_request_builder + validation.final_rca_validator
validation.final_rca_validator → schemas/final_rca.schema.json
```

External dependencies (from `requirements.txt` and `pyproject.toml`):
- `pyyaml>=6.0` — taxonomy loader.
- `pytest>=7.0` — test runner.
- `jsonschema>=4.20` — schema validation.
- Python `>=3.11` (dataclass(frozen=True), `str | None` syntax).
- `git` binary on PATH (subprocess, not a Python dependency).
- No HTTP frameworks; stdlib `http.server` only.

---

## 5. Protected Modules

The following are **frozen** in this audit and require a documented,
backward-compatible justification to modify:

| Path | Owner | Reason it's protected |
|---|---|---|
| `engine/` (all) | Decision authority | All RCA reasoning. Any silent change is a regression. |
| `extractors/` | Observation contract | Defines `Observation` shape consumed by every downstream layer. |
| `evidence/evidence_builder.py` | Classification rules bridge | Single point that turns raw observations into classified evidence. |
| `rules/rules.config.json` | Decision weights | Changing weights changes scores without code change — high regression risk. |
| `taxonomy/taxonomy.yaml` | Failure-type catalogue | The taxonomy is referenced from `RulesConfig`, severity_policy, fingerprint. |
| `schemas/*.json` | Public contracts | jsonschema files are validated by tests/test_schema_validation.py. |
| `validation/final_rca_validator.py` | LLM boundary guard | The 6 invariants must hold or the LLM can corrupt decisions. |
| `cli/main.py` | Backward-compat surface | Documented CLI behavior; tests depend on output. |
| `collectors/` | Source-of-truth interface | Shape `{source_name: text}` is the contract with `AnalysisPipeline`. |
| `pipeline.py` | Orchestrator | The orchestrator's contract is the dataclass shapes and field meanings. |

### Allowed future modifications (with constraints)
- **Adding** new fields/extractors/collectors/evidence kinds: allowed if
  `additionalProperties: false` schemas are version-bumped (`schema_version`
  + a new schema file) **or** the new field is additive and `Observation.to_dict()`
  carries it.
- **Modifying** existing fields: requires `schema_version` bump + migration.
- **Replacing** any decision logic: forbidden.

---

## 6. Extension Points

These are the seams where Phase 1 (and beyond) can plug in without
touching protected code:

### 6.1 Collectors (in `collectors/`)
- New collectors implement a `collect(...) -> str` (single file) or
  `collect_diff/commit/branch/...` (git-like) interface.
- They return `Dict[str, str]` keyed by source name, fed into
  `PipelineInput.sources`.
- Existing `GitCollector`, `FileCollector` set the precedent. New ones
  (`DockerLogCollector`, `DockerEventCollector`, `DockerMetricsCollector`,
  `HostMetricsCollector`, `ElasticsearchCollector`, `PrometheusCollector`,
  `KubernetesCollector`, `CICDCollector`, `GitHubCollector`, `GitLabCollector`)
  follow the same pattern.

### 6.2 Extractors (in `extractors/`)
- New extractor = subclass `BaseExtractor` + `@registry.register(...)`
  with `ExtractorMetadata`.
- Existing kinds enum (`VALID_OBSERVATION_KINDS`) is additive —
  extending it requires updating the schema enum and the test that
  validates it, but no engine change.

### 6.3 Classification rules (`rules/rules.config.json`)
- `classification_rules[]` is already designed for new `CRxxx` entries.
- New rule → new evidence → may produce additional corroboration.
- **Must not break symmetry** of `additional_evidence_weight`
  (documented invariant from Bug #4).

### 6.4 Hypothesis catalog (`rules/rules.config.json` + `taxonomy/taxonomy.yaml`)
- Adding a new failure type is additive: catalog entry + taxonomy entry +
  fix_hint + remediation_engine branch.
- Validation in `RulesConfig.from_dict` ensures the catalog is internally
  consistent.

### 6.5 LLM providers (in `llm/`)
- Implement `LLMClient` Protocol; nothing else changes.
- The current OpenAI-compatible client is already protocol-driven.

### 6.6 Serializers (`api/serializers.py`)
- `investigation_payload` is the single canonical payload builder.
- New sections can be added by appending to `PipelineResult` first, then
  surfacing in the payload.

### 6.7 UI tabs (`web/static/index.html` + `app.js`)
- New tabs follow the existing pattern: `<div class="tab-panel" data-tab="X">` + `renderX(...)` function.

---

## 7. Current Risks

| Risk | Severity | Mitigation already in place |
|---|---|---|
| Corroboration without dedup (TD-001) | Low | Each extractor internally dedupes (seen_keys / seen_modules / seen_signatures) |
| Flat `Observation.kind` enum (TD-002) | Low | v2 deferred; family/kind scheme is acceptable if ever needed |
| `models/*.py` not auto-generated from schemas (TD-003) | Low | Tests assert schema conformance at runtime |
| Fixtures inlined in `test_smoke.py` (TD-004) | Low | Acceptable for current test count |
| dataclasses vs pydantic (TD-005) | Low | Avoid extra deps in core; FastAPI never became a dependency |
| `Observation.kind` enum + `source` enum rigid for new collectors | Medium | Will need version-bumped schema files for new kinds; planned ahead in §6 |
| In-memory investigation store (`InvestigationService`) | Medium | Process-local only — no persistence, no multi-process. Acceptable for the demo but documented as a boundary. |
| No auth on `/api/v1/*` | High (future) | Already guarded by workspace allowlist (`AUTORCA_WORKSPACE_ROOT`); see §9. |
| Single-process HTTP server (`BaseHTTPRequestHandler`) | Low | Fine for demo; not for production scale. |
| `cli/main.py` opens `pipeline._rule_engine` and `pipeline._scoring_engine` (private attrs) to build RCARequestBuilder | Low | Cross-module private access — fragile but confined to two callers. |

---

## 8. Security Risks

| Area | Current state | Future Phase 1 must |
|---|---|---|
| Filesystem access | `FileCollector` reads any file path passed to it; **no allowlist in CLI** | Bound collector paths to `AUTORCA_WORKSPACE_ROOT` (already done in API; not in CLI). |
| Workspace allowlist | `api/security.py` validates parent path is inside `AUTORCA_WORKSPACE_ROOT` (default `projects-for-test`). `parent.resolve(strict=False)` accepts symlinks *placed inside* the workspace. | Confirm symlink policy explicitly. Forbid parent traversal via `..`. |
| Path traversal | API: validated. CLI: `args.traceback` is consumed verbatim. | CLI must use same allowlist (or document why it's privileged). |
| Symlinks | Allowed when placed by trusted caller inside workspace. | Document threat model; consider `realpath` check. |
| Environment masking | `api/security.mask_secret` redacts sensitive env-var names in observation `data`. | Extend to **all** env-var-like fields (currently checks `key`, `missing_key`, `env_var`, `variable`, `name`). |
| Subprocess execution | `GitCollector` calls `git` via subprocess with a list of args (`["git", "-C", path, "show", "--format=", commit]`). No shell=True. | Keep list-form. New collectors must use list-form. |
| Docker socket access | None today. | Phase 1 may add `DockerLogCollector`. Must be opt-in (config), never auto-connect, never read `/var/run/docker.sock` without explicit env var. |
| Future API credentials | `AUTORCA_LLM_API_KEY` read from env; never logged. | Continue: never log secrets; never serialise into responses. |
| Auth / authz | None on HTTP endpoints. Loopback assumed. | When exposing beyond loopback, add bearer-token middleware **before** any external collector reads. |
| LLM prompt injection | LLM only receives `RCARequest` JSON. | Keep it that way. Do **not** ever pass raw `sources` to the LLM. |
| Network egress | Only `OpenAICompatibleLLMClient` makes outbound. | Allowlist outbound destinations via config. |
| File uploads | API caps request body at 256 KiB (`web_app.py:258`). | Keep this cap. Add per-field size limit if `traceback` could grow. |
| Static asset serving | `_serve_static` validates that resolved path is inside `STATIC_ROOT`. | Document this is the only path to disk from the HTTP layer besides `/api/*`. |

### 8.1 Threat model (concise)
- **Trusted caller**: writes investigations and chooses `repo` paths inside
  the workspace. Expected behaviour: human operator or a trusted CI bot.
- **Untrusted caller**: same network. **Threat**: forces pipeline to read
  arbitrary paths, exfiltrate files, or pivot through subprocess.
- **Mitigations already present**: workspace allowlist, symlink policy,
  request size cap, environment-variable value masking, no shell=True.
- **Mitigations required before exposing the API on a non-loopback
  interface**: auth, per-investigation audit log, rate limiting, and a
  tightened `realpath` policy.

---

## 9. Technical Debt (cumulative)

Carried forward from `docs/TECHNICAL_DEBT.md`, with explicit status:

- **TD-001** Corroboration dedup — accepted; extractors dedupe already.
- **TD-002** `Observation.kind` flatness — accepted; defer.
- **TD-003** Schema conformance is tested at runtime, not enforced by generated models — accepted; safe.
- **TD-004** Inlined fixtures in `test_smoke.py` — accepted; would change at productionisation.
- **TD-005** dataclasses vs pydantic — accepted; keeps core dependency-free.
- **TD-006** `final_rca_validator.py` complete — closed.

**New debt surfaced by this audit:**

- **TD-101** `cli/main.py` reaches into `pipeline._rule_engine`, `pipeline._scoring_engine`, `pipeline._taxonomy_index`, `pipeline._rules_config` private attrs to construct `RCARequestBuilder`. Same in `web_app.py:114-118`. The orchestrator should expose a public method.
- **TD-102** `analyze_incident` in `web_app.py` is duplicated logic with `InvestigationService.create_investigation`. The legacy `/api/analyze` endpoint re-implements the same control flow. Acceptable as a backward-compat shim, but it should be the **only** place that does this, and tests cover it.
- **TD-103** `_investigation_id` in `web_app.py:34` and `_timestamp_id` in `cli/main.py:201` produce slightly different formats (`AR%Y%m%d-%H%M%S%f` vs `AR%Y%m%d-%H%M%S`). They never meet because CLI and HTTP never share state. Not a correctness issue, but worth aligning.
- **TD-104** `mask_secret` recognises a fixed list of var-name patterns; if a future extractor produces a sensitive value under a different key (e.g. `secret_value`), it will not be masked. Worth a generic "does this look like a secret assignment?" gate (`looks_like_secret_assignment` exists but is unused).
- **TD-105** No multi-process safety on `InvestigationService._investigations` — but the store is process-local and the lock is held only for dict ops. Adequate for stdlib server.
- **TD-106** `rca_request_builder._build_log_excerpt` calls `str(value)` on regex `raw_ref.strip()` and uses substring match. For very large logs (multi-MB), this is O(n × m). Adequate today given `max_log_lines=40` truncation.

---

## 10. Recommended Phase 1 Architecture

The target architecture in the prompt is essentially what AutoRCA already
implements, with one structural addition: a **`Collector` abstraction
with normalised output** so future collectors don't require pipeline
changes.

### 10.1 Future flow (extends, does not replace)

```
Incident Sources                                  (read-only, external)
   - Docker (logs / events / metrics)
   - Prometheus (query results)
   - Elasticsearch (log search)
   - GitHub / GitLab (workflow runs / commit metadata)
   - Kubernetes (pod events, container logs)
   - CI/CD (workflow run logs)
   - Alerts / events (PagerDuty, OpsGenie, generic webhook)
        │
        ▼
Collectors                                        (new code)
   - GitCollector             (existing)
   - FileCollector            (existing)
   - DockerLogCollector       (NEW — wraps `docker logs`)
   - DockerEventCollector     (NEW — wraps `docker events`)
   - DockerMetricsCollector   (NEW — wraps `docker stats`)
   - HostMetricsCollector     (NEW — /proc, /sys, ss, free)
   - ElasticsearchCollector   (NEW — POST _search)
   - PrometheusCollector      (NEW — GET /api/v1/query)
   - KubernetesCollector      (NEW — kubectl / Python client)
   - CICDCollector            (NEW — GH Actions / GitLab CI)
        │
        ▼  (all return Dict[str, str] — same shape)
PipelineInput.sources                             (existing contract)
        │
        ▼
Extraction → Evidence → RuleEngine → Timeline → Correlation → Graph →
Fingerprint → Remediation → HypothesisAssessment    (existing pipeline)
        │
        ▼
Investigation Platform                             (existing UI + API)
        │
        ▼
AI Explanation                                     (existing LLM layer)
        │
        ▼
Safe Remediation                                   (existing, with policy hooks)
```

### 10.2 Critical invariants preserved
- Deterministic RCA is the decision authority — never replaced.
- All new collectors must emit text/JSON-shaped `Dict[str, str]` so they
  drop into existing `PipelineInput.sources` without new schema work.
- New "kinds" require a coordinated schema bump (`observation.schema.json`
  enum + `extractors/base.VALID_OBSERVATION_KINDS`).
- New collectors must respect the workspace allowlist.

### 10.3 New types introduced in this audit (for future Phase 1)
- **`IncidentContext`** — proposed for §8 of the brief, but evaluation
  shows the current `PipelineInput` + `Investigation` already cover the
  needed fields (incident_id, repo, environment, commit_sha, branch,
  full_name, sources are implicit). **Recommendation:** do not introduce
  a new top-level context object yet. Extend `PipelineInput` with optional
  `service`, `start_time`, `end_time`, `deployment` fields (defaulting
  to None) when a real collector needs them. This keeps the existing
  dataclass frozen + additive.
- **`EvidenceModelV2`** — current `Evidence` (dict) is already additive.
  Any new field (`source`, `timestamp`, `service`, `resource`, `type`,
  `raw_reference`, `normalized_value`, `confidence`, `observation_id`)
  is either already present (`source`, `type`, `raw_reference`,
  `observation_id`) or can be added with a `schema_version` bump on the
  evidence schema. `confidence` is **not** to be added to Evidence
  (confidence belongs to the Hypothesis, not to Evidence); do not
  conflate.
- **`RootCause` / `ContributingFactor` / `Symptom`** — the current
  domain model has only `Hypothesis` and `HypothesisAssessment`. The
  `HypothesisAssessment` already separates `supporting_evidence_ids`
  from `contradicting_evidence_ids`, which is a primitive form of the
  desired distinction. **Recommendation:** Phase 1 may introduce a
  thin `EvidenceRole` enum on `HypothesisAssessment.links` (currently
  `relation` ∈ {`supports`, `contradicts`}). Mapping:
  - `supports` Evidence → **Contributing Factor** (causal, not the root)
  - `contradicts` Evidence → **Symptom / Contradiction** (incompatible
    with this hypothesis)
  - Selected failure_type → **Root Cause** (deterministically chosen)
  This stays inside `HypothesisAssessment.links.relation` and avoids a
  second copy of the model.

---

## 11. Migration Strategy

For Phase 1, the only file additions allowed in this audit are:

| New file | Purpose | Backward compat |
|---|---|---|
| `collectors/docker_log_collector.py` | wrap `docker logs <container>` | Returns `Dict[str, str]` with source `docker_output`. New schema kind optional. |
| `collectors/docker_event_collector.py` | wrap `docker events` | Same. |
| `collectors/docker_metrics_collector.py` | wrap `docker stats --no-stream` | New source `docker_metrics`; needs a new `observation.schema.json` enum value (`host_metrics` / `container_metrics` kind) and a `extractors/base.VALID_OBSERVATION_KINDS` addition. |
| `collectors/host_metrics_collector.py` | reads `/proc/meminfo`, `free`, etc. | Same. |
| `collectors/prometheus_collector.py` | HTTP POST/GET to Prom | Requires `AUTORCA_PROM_URL`, `AUTORCA_PROM_TOKEN` env vars; must be opt-in. |
| `collectors/elasticsearch_collector.py` | HTTP to ES | Same. |
| `collectors/kubernetes_collector.py` | wraps `kubectl logs` / Python client | Requires kubeconfig; opt-in. |
| `collectors/cicd_collector.py` | wraps GH Actions REST / GitLab CI API | Requires token; opt-in. |
| `collectors/github_collector.py` / `collectors/gitlab_collector.py` | wraps SCM REST API for commit metadata | Opt-in. |

All new collectors must:
1. Live under `collectors/`.
2. Return `Dict[str, str]`.
3. Validate any path / URL through `api/security.resolve_repository_path`
   or an analogous `resolve_target_url` helper.
4. Never log secrets.
5. Pass through the existing `ExtractorRegistry` mechanism for parsing.

### 11.1 Schema version policy
- `observation.schema.json` enum is additive: new kind → bump
  `schema_version` + add enum entry + add `extractors/base.VALID_OBSERVATION_KINDS`
  + add extractor that produces it.
- Same for `evidence.schema.json`, `hypothesis.schema.json`.
- `rca_request.schema.json` and `final_rca.schema.json` are also additive.
  If a contract must change shape, bump schema_version and ship a new
  file alongside the old.

---

## 12. Testing Strategy

The 114-test baseline is the contract. New tests in Phase 1 must:

1. **Not break** any existing test. Tests run from the project root:
   `python3 -m pytest -q`.
2. **Cover each new collector** with at least:
   - Happy-path against a mocked/fake external source.
   - Failure path (unavailable host, auth error, empty response).
   - Workspace-allowlist rejection when applicable.
3. **Cover each new observation kind** with:
   - Extractor unit test (regex match).
   - End-to-end pipeline run produces the expected evidence.
   - Schema validation against `observation.schema.json` (the
     `test_schema_validation.py` pattern).
4. **Cover new endpoints** with the same `ThreadingHTTPServer` + real
   `AnalysisPipeline` pattern from `test_api_investigations.py`.
5. **Cover the LLM boundary** by mutating `FakeLLMClient` responses
   (`test_llm_integration.py` pattern) to confirm the 6 invariants hold
   if a new collector's evidence shows up in `RCARequest`.

### 12.1 Coverage expectations by layer
| Layer | Current | Target after Phase 1 |
|---|---|---|
| Deterministic core (extractors, evidence, engines) | high | unchanged |
| Pipeline orchestration | high | unchanged |
| LLM boundary | high | unchanged |
| API + Investigation UI | medium-high | unchanged + new tests per new endpoint |
| Real E2E (target demo) | exists | must add one new E2E test per new collector type |

---

## 13. Real E2E Strategy

The target demo is `projects-for-test/python-fastapi-demo-docker`. It is
**external** to AutoRCA — used only for controlled real-incident
generation and E2E validation. AutoRCA must not hardcode any reference
to it (verified: `grep -r "python-fastapi-demo" --include="*.py" …` only
hits `tests/test_api_investigations.py` and
`validate_real_incident.py`, both test entry points).

Phase 1 E2E protocol:

1. **Spin up the FastAPI demo** in a clean environment using its own
   `docker-compose.yml` (the project ships one — no AutoRCA involvement).
2. **Reproduce a controlled incident** by reverting a known commit or
   removing an env var from `.env`.
3. **Collect evidence** using the new collector(s) being tested.
4. **Run AutoRCA's InvestigationService** against the collected evidence.
5. **Assert** that the resulting `selected_hypothesis` matches the
   pre-determined incident classification.
6. **Verify** that without `--llm`, the LLM boundary is not crossed
   and the deterministic decision is preserved end-to-end.

### 13.1 Real-incident artefacts
- `real_scenarios.py` already provides three controlled scenarios
  (missing env, missing dependency, port conflict) that run real
  Python processes and produce real tracebacks. These remain the
  primary E2E harness for the core.
- The FastAPI demo remains the integration target for Docker-collector
  scenarios when those are added.

---

## 14. Files That SHOULD NOT Be Modified

(Phase 1 implementation must not touch these without an explicit
backward-compatibility review and updated tests.)

### 14.1 Frozen engine code
- `engine/rule_engine.py`
- `engine/scoring_engine.py`
- `engine/hypothesis_engine.py`
- `engine/correlation_engine.py`
- `engine/timeline_engine.py`
- `engine/incident_graph.py`
- `engine/incident_fingerprint.py`
- `engine/remediation_engine.py`

### 14.2 Frozen extractor contract
- `extractors/base.py` (the `Observation`, `Location`, `ExtractionContext`
  dataclasses and the `BaseExtractor` ABC are public contracts)
- `extractors/registry.py` (registry protocol)
- `extractors/missing_env_extractor.py`
- `extractors/missing_dependency_extractor.py`
- `extractors/port_conflict_extractor.py`
- `extractors/diff_extractor.py`

### 14.3 Frozen validation
- `evidence/evidence_builder.py`
- `validation/final_rca_validator.py`

### 14.4 Frozen schemas (existing files)
- `schemas/observation.schema.json`
- `schemas/evidence.schema.json`
- `schemas/hypothesis.schema.json`
- `schemas/rca_request.schema.json`
- `schemas/final_rca.schema.json`

Additive schema bumps ship as **new files** (`observation.v2.schema.json` etc.).

### 14.5 Frozen taxonomy + rules
- `taxonomy/taxonomy.yaml`
- `rules/rules.config.json`
- `config/rules_config.py`

### 14.6 Frozen CLI + collector core
- `cli/main.py` (existing commands and argument set)
- `collectors/git_collector.py` (its public methods are reused)
- `collectors/file_collector.py` (its public methods are reused)

### 14.7 Frozen pipeline orchestrator surface
- `pipeline.py` — the **public surface** (`PipelineInput`, `PipelineResult`,
  `AnalysisPipeline.from_config_files`, `AnalysisPipeline.run`) must not
  change shape. New optional fields with default values are allowed.

---

## 15. Files That MAY Be Extended (with care)

| Path | Allowed extension | Forbidden change |
|---|---|---|
| `collectors/` | Add new files | Modify `git_collector.py` / `file_collector.py` public methods |
| `extractors/` | Add new files; add to `VALID_OBSERVATION_KINDS` (with schema bump) | Change `Observation` field set |
| `engine/` | Add new engine modules | Modify existing engines' decision logic |
| `taxonomy/` | Add new FT entries with status="implemented" | Modify existing FT entries in breaking ways |
| `rules/rules.config.json` | Add new `CRxxx` rules, new hypothesis catalog entries, new severity rules | Change scoring weights / clamps without regression tests |
| `schemas/` | Add new files (v2) | Modify existing files (additive only) |
| `rca_request/rca_request_builder.py` | New optional fields, new builder helper methods | Re-derive score/confidence anywhere |
| `api/investigation_service.py` | Add new optional inputs, new endpoints | Change existing 4xx semantics |
| `api/serializers.py` | New serialisers for new payload sections | Remove or rename existing fields |
| `api/security.py` | Add new validators | Loosen existing validations |
| `web_app.py` | Add new routes | Change existing route semantics |
| `web/static/` | New tabs, new CSS, new JS | Rename existing IDs the UI tests grep for |
| `tests/` | Add new tests | Modify existing tests' expectations (add new ones instead) |

---

## 16. Phase 1 Implementation Plan (sketch, NOT to execute now)

The plan is sequenced so the green baseline is preserved at every step.

### 16.1 Goals (in order)
1. **New collector abstractions** (no behavior change to pipeline).
2. **New collector implementations** behind opt-in config.
3. **PipelineInput additions** for service / start_time / end_time / deployment
   (defaulting to None).
4. **HypothesisAssessment.links** gains an additional optional `role`
   field (root_cause | contributing_factor | symptom) when a real
   collector demonstrates the need; otherwise this stays as a future
   schema version.
5. **E2E test** for each new collector against the FastAPI demo.

### 16.2 Pre-flight checklist (must hold before any code change)
- [ ] `python3 -m pytest -q` → 114 passed.
- [ ] No uncommitted changes in `engine/`, `extractors/`, `evidence/`,
      `validation/`, `rules/`, `taxonomy/`, `schemas/`, `pipeline.py`,
      `cli/main.py`, `collectors/git_collector.py`,
      `collectors/file_collector.py`.
- [ ] `python3 -c "from pipeline import AnalysisPipeline, PipelineInput, PipelineResult; print('ok')"`
- [ ] `python3 -m cli.main analyze --help` prints expected usage.

### 16.3 Sequence (each step = one PR with tests)

1. **PR-1: Collector protocol (no behaviour change)**
   - New file `collectors/base.py` defining a `CollectorProtocol`
     (Protocol with `collect() -> Dict[str, str]`).
   - New tests asserting `GitCollector` and `FileCollector` satisfy it.
   - Baseline must remain 114 + N new tests.

2. **PR-2: Docker collectors (opt-in)**
   - `collectors/docker_log_collector.py`, `docker_event_collector.py`,
     `docker_metrics_collector.py`.
   - CLI flag `--docker-host` for opt-in (env var
     `AUTORCA_DOCKER_HOST`).
   - New kinds: `docker_event`, `docker_metrics`. Schema bump
     `observation.schema.json` → v2 alongside v1.
   - Tests against a stub Docker daemon (no real socket in CI).

3. **PR-3: Prometheus collector (opt-in)**
   - `collectors/prometheus_collector.py`. Env vars `AUTORCA_PROM_URL`,
     `AUTORCA_PROM_TOKEN`.
   - Network allowlist check.

4. **PR-4: Elasticsearch + Kubernetes + CI/CD collectors**
   - Same pattern, all opt-in via env vars.

5. **PR-5: PipelineInput extension**
   - Add optional fields: `service`, `start_time`, `end_time`,
     `deployment` (all defaulting to None).
   - `InvestigationService` passes them through when present.
   - Tests cover default behaviour unchanged.

6. **PR-6: Real-incident E2E with FastAPI demo**
   - Test that runs the FastAPI demo container, breaks it, collects
     evidence via Docker log collector, runs the pipeline, asserts the
     expected hypothesis.

### 16.4 Stop conditions
- Any PR that breaks the 114-test baseline must be reverted and re-scoped.
- Any PR that requires modifying a frozen file (see §14) is rejected at
  design review.
- Any PR that introduces an LLM call inside the deterministic core is
  rejected unconditionally.

---

## 17. Stop — Awaiting Implementation Instruction

This audit is complete. The architecture is frozen. No implementation
has occurred. The next phase requires explicit user direction.
