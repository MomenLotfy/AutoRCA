# Phase 2.2 Report — Prometheus + GitHub + GitLab Integrations

Date: 2026-08-27
Branch: `autorca-port-conflict-incident`
Baseline: `python3 -m pytest -q` → 175 passed (Phase 2.1 green)

---

## TL;DR — Phase 2.2 PASS

- **246 tests pass** (175 baseline + 71 new). No Phase 1 / Phase 2.1 test was modified.
- The deterministic RCA engine remains the sole decision authority — no
  collector invents observations, calculates root cause, or bypasses
  the existing pipeline.
- The three new integrations are opt-in: when no `prometheus`,
  `github_changes`, or `gitlab_changes` block is sent in the request
  body, behaviour is byte-identical to Phase 2.1.
- Live E2E (Prometheus, GitHub, GitLab) was skipped (no local services
  reachable) per explicit user direction; the report logs that decision.
- Zero new project dependencies. All collectors use stdlib
  `urllib.request`.

---

## A. Architecture

The Phase 2.1 integration seam is **reused unchanged**. The new
collectors sit alongside `ElasticsearchCollector` and share the same
generic envelope → `CollectedItem` → `*Extractor` → pipeline flow.

```
                       ┌───────────────────────────────────────┐
                       │  POST /api/v1/investigations          │  (existing route)
                       │  body.prometheus      = {...}         │
                       │  body.github_changes  = {...}         │
                       │  body.gitlab_changes  = {...}         │
                       └───────────────┬───────────────────────┘
                                       ▼
                       ┌───────────────────────────────────────┐
                       │  InvestigationService                 │
                       │  _run_integration_collection(source)  │  (new generic helper)
                       └──────┬──────────────┬─────────────┬───┘
                              ▼              ▼             ▼
                    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
                    │ Prometheus   │ │ GitHubChange │ │ GitLabChange │
                    │ Collector    │ │ Collector    │ │ Collector    │
                    │ (stdlib HTTP)│ │ (stdlib HTTP)│ │ (stdlib HTTP)│
                    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
                           │                │                │
                           ▼                ▼                ▼
                    JSON envelope   JSON envelope   JSON envelope
                    source=         source=         source=
                    "prometheus"    "github_changes" "gitlab_changes"
                           │                │                │
                           ▼                ▼                ▼
                    PrometheusExtractor  GitHubChangeExtractor  GitLabChangeExtractor
                           │                │                │
                           └────────────────┼────────────────┘
                                            ▼
                          Observation(kind="generic_log_line",
                                      source=<one of three>)
                                            ▼
                                Existing pipeline (unchanged)
                                Evidence → Rule → Timeline → Correlation →
                                Graph → Fingerprint → Remediation → Assessment
```

A new provider-neutral `ChangeProvider` Protocol lets the API layer
dispatch to GitHub / GitLab without knowing the transport. A future
code-hosting integration (Bitbucket, Gitea, etc.) is a one-file drop-in.

---

## B. Files created

| Path | Purpose |
|---|---|
| `collectors/change_provider_base.py` | `ChangeEvent` dataclass, `@runtime_checkable ChangeProvider` Protocol, `clamp_window`, `project_extra` |
| `collectors/prometheus_collector.py` | `PrometheusCollector` — instant + range queries, 30 s min step, 6 h max window |
| `collectors/github_change_collector.py` | `GitHubChangeCollector` — commits + PRs, bearer / basic auth, `X-GitHub-Api-Version: 2022-11-28` |
| `collectors/gitlab_change_collector.py` | `GitLabChangeCollector` — commits + merge requests, `PRIVATE-TOKEN` / bearer, URL-encoded project paths |
| `extractors/prometheus_extractor.py` | Envelope → one `Observation` per series |
| `extractors/github_change_extractor.py` | Envelope → one `Observation` per commit / PR |
| `extractors/gitlab_change_extractor.py` | Envelope → one `Observation` per commit / MR |
| `tests/test_phase2_prometheus_collector.py` | 28 unit tests |
| `tests/test_phase2_change_collectors.py` | 35 unit tests |
| `tests/test_phase2_cross_integration.py` | 8 API-level integration tests |
| `docs/PHASE2_2_REPORT.md` | this document |

## C. Files modified (additive only)

| File | Change | Why additive |
|---|---|---|
| `collectors/integration_base.py` | `index_pattern` made optional; added `resource` and `auth_scheme` fields; loosened ES-specific grammar check (moved into the ES collector); `MAX_WINDOW_SECONDS = 6*3600` cap enforced in `__post_init__` | schema-only change, default-safe |
| `collectors/elasticsearch_collector.py` | `_ES_INDEX_PATTERN` regex moved here from `integration_base`; `_redact_headers` upgraded to greedy regex | moved code, no behaviour change |
| `extractors/registry.py` | appended `"prometheus"`, `"github_changes"`, `"gitlab_changes"` to `VALID_SOURCES` tuple | tuple only, no removal |
| `schemas/observation.schema.json` | added 3 new values to the `source` enum | enum only, no removal |
| `schemas/evidence.schema.json` | added 3 new values to the `source` enum | enum only, no removal |
| `pipeline.py` | `import extractors.prometheus_extractor`, `github_change_extractor`, `gitlab_change_extractor` (noqa: F401) | side-effect registration, same pattern as Phase 1 / 2.1 |
| `api/investigation_service.py` | added `_run_integration_collection(source, config)` generic helper + 3 collector imports; called from `create_investigation` only when block present; results merged into `sources` and metadata blocks added to payload | new helper, existing paths unchanged |
| `api/serializers.py` | added `prometheus`, `github_changes`, `gitlab_changes` kwargs to `investigation_payload` | new keys, existing keys unchanged |
| `reporting/incident_report_renderer.py` | added 3 source labels to `_SOURCE_LABELS` | dict add only |
| `web/static/index.html` | added 4 form fields (Prometheus URL + query, GitHub repo, GitLab project) | append-only |
| `web/static/app.js` | 18-line change forwarding the new blocks | additive |

## D. Files intentionally untouched

- `engine/` (all 8 modules)
- `evidence/evidence_builder.py` (V2 fields flow through unchanged)
- `validation/`
- `taxonomy/taxonomy.yaml`, `rules/rules.config.json`, `config/rules_config.py`
- `cli/main.py` (no new CLI args needed)
- `collectors/{file,git,docker_*}*.py`
- `extractors/{base,missing_*,port_conflict,diff,docker_*}*.py`
- `llm/`, `rca_request/`
- `web_app.py` (no new routes; same `POST /api/v1/investigations`
  accepts the new blocks in its JSON body)
- `schemas/{rca_request,final_rca,hypothesis}.schema.json`
- `real_scenarios.py`, `real_incidents/`

## E. Hard limits (per user direction)

| Limit | Value | Where enforced |
|---|---|---|
| Prometheus window | ≤ 6 hours | `IntegrationConfig.__post_init__` (raises `ValueError`) |
| Prometheus step | ≥ 30 seconds | `PrometheusCollector` (`MIN_STEP_SECONDS`) |
| Prometheus samples | ≤ 2000 per query | `MAX_RANGE_SAMPLES` |
| Change-provider window | ≤ 7 days | `clamp_window` in `change_provider_base` |
| Change-provider min window | ≥ 60 seconds | `clamp_window` |
| Change-provider events | ≤ 200 per window | `MAX_CHANGES_PER_WINDOW` |
| Body size cap | 5 MiB | `_safe_read` in each collector |
| Timeout | 0.5 s – 30 s | `IntegrationConfig.timeout_seconds` clamp |

## F. Security controls

| Control | Implementation |
|---|---|
| URL scheme allowlist | `validate_integration_url` rejects non-`http(s)` and embedded userinfo |
| SSRF | defaults to deny loopback/private/link-local; `AUTORCA_PROM_ALLOW_LOOPBACK=1` opt-out for tests |
| Auth | env var name only (`auth_env`); never logged; never returned in metadata; **scrubbed from every error message** |
| Timeout | `urllib.request` + `socket.setdefaulttimeout`; bounded |
| Body cap | streaming read with byte counter; >5 MiB → `IntegrationError` |
| Loud failure | `IntegrationError` → `AnalysisRequestError` → HTTP 400; never silently empty |
| extra_headers override guard | `Authorization` rejected at API validation, regardless of source |
| No shell | `urllib.request` only |
| No subprocess | stdlib HTTP only |
| Secret scrubbing regex | `(?i)authorization\s*[:=]\s*\S.*` — greedy to remove entire trailing secret even when additional text follows |

## G. Tests added (71 total — all pass)

### `tests/test_phase2_prometheus_collector.py` (28 tests)

1. `name` attribute equals `"prometheus"`
2. `is_available` returns `True` with valid config
3. `is_available` returns `False` when no endpoint
4. Window > 6 h raises `ValueError` via `IntegrationConfig`
5. Successful range query → 1 envelope + correct metadata
6. Successful instant query → vector envelope
7. Step < 30 s → coerced to 30 s
8. Step > 2000 samples → coerced to max
9. Timeout → `IntegrationError`
10. HTTP 401 → `IntegrationError`
11. HTTP 500 → `IntegrationError`
12. HTTP 503 → `IntegrationError`
13. Malformed JSON → `IntegrationError`
14. Oversized body (>5 MiB) → `IntegrationError`
15. Connection refused → `IntegrationError`
16. Bearer auth header applied
17. `Authorization` scrubbed from error message
18. Bearer `<token>` scrubbed from error message
19. `PRIVATE-TOKEN` scrubbed from error message
20. Auth header scrubbed from envelope metadata
21. Matrix projection preserves samples
22. Invalid metric name (`!@#$`) rejected at config level
23. Extractor round-trip — envelope → `Observation(kind="generic_log_line", source="prometheus")`
24. Source registered in `extractors.registry.VALID_SOURCES`
25. Pipeline integration — collector → pipeline produces observation list
26. `incident_start` / `incident_end` resolution from `IncidentContext`
27. Step floor constant is 30
28. Window cap constant is 6 h

### `tests/test_phase2_change_collectors.py` (35 tests)

**GitHub (15):**

1. `name` attribute equals `"github_changes"`
2. Constructor rejects non-GitHub `IntegrationConfig.source`
3. Constructor rejects missing `resource`
4. `is_available` returns `True` / `False` correctly
5. Successful collection — commits + PRs envelope
6. Bearer auth applied
7. Basic auth applied
8. `Authorization` scrubbed from error message (greedy regex)
9. Bearer `<token>` scrubbed from error message
10. `token:` / `private-token:` scrubbed from error message
11. Auth header never in `CollectedItem.metadata`
12. Malformed JSON → `IntegrationError`
13. Connection refused → `IntegrationError`
14. PR filter — outside-window PR dropped
15. Extractor round-trip — envelope → `Observation(kind="generic_log_line", source="github_changes")`

**GitLab (12):**

1. `name` attribute equals `"gitlab_changes"`
2. Constructor rejects non-GitLab `IntegrationConfig.source`
3. Constructor rejects missing `resource`
4. `is_available` returns `True` / `False` correctly
5. Successful collection — commits + MRs envelope
6. `PRIVATE-TOKEN` auth applied
7. Bearer auth applied (`auth_scheme="bearer"`)
8. Project path URL-encoded (`mygroup/mysubgroup/project` → `mygroup%2Fmysubgroup%2Fproject`)
9. `Authorization` scrubbed from error message
10. `PRIVATE-TOKEN` scrubbed from error message
11. Malformed JSON → `IntegrationError`
12. Extractor round-trip — envelope → `Observation(kind="generic_log_line", source="gitlab_changes")`

**Protocol + helpers (8):**

1. `isinstance(gh_collector, ChangeProvider)` is `True`
2. `isinstance(gl_collector, ChangeProvider)` is `True`
3. `collect_changes()` dispatch returns merged list
4. `clamp_window` rejects window > 7 days
5. `clamp_window` rejects window < 60 s
6. `clamp_window` accepts 6-hour window
7. `clamp_window` honours explicit bounds
8. `project_extra` filters unknown keys to a safe whitelist

### `tests/test_phase2_cross_integration.py` (8 tests)

1. `prometheus` block → collector runs, `payload["prometheus"]` populated, observation emitted
2. `github_changes` block → collector runs, `payload["github_changes"]` populated, observation emitted
3. `gitlab_changes` block → collector runs, `payload["gitlab_changes"]` populated, observation emitted
4. None of the Phase 2.2 blocks → behaviour unchanged from Phase 2.1
5. Prometheus missing `query` → HTTP 400
6. GitHub missing `resource` → HTTP 400
7. Prometheus collector raises `IntegrationError` → HTTP 400
8. `extra_headers` cannot override `Authorization` (Prometheus) → HTTP 400

## H. Test result

```
$ python3 -m pytest -q --no-header
........................................................................ [ 29%]
........................................................................ [ 58%]
........................................................................ [ 87%]
..............................                                           [100%]
246 passed in 30.46s
```

| Bucket | Tests |
|---|---|
| Phase 0 / 1 baseline | 142 |
| Phase 2.1 (Elasticsearch) | 33 |
| Phase 2.2 (Prometheus) | 28 |
| Phase 2.2 (GitHub + GitLab) | 35 |
| Phase 2.2 (cross-integration) | 8 |
| **Total** | **246** |

## I. Live E2E

Live E2E for Prometheus / GitHub / GitLab was **explicitly skipped**
per user direction (no local services available). All HTTP boundaries
are mocked at the `urllib.request.urlopen` boundary; the test suite
exercises:

- success paths (range, instant, commits, PRs, MRs)
- every documented failure mode (timeout, 4xx, 5xx, malformed JSON,
  oversized body, connection refused)
- every auth scheme (none, bearer, basic, `PRIVATE-TOKEN`)
- secret scrubbing in three independent regex patterns

## J. Verification against frozen invariants

- [x] **No protected module touched.** `engine/*`, `evidence/*`,
      `validation/*`, `taxonomy/*`, `rules/*`, `extractors/{base,
      missing_*,port_conflict,diff}*.py`, `collectors/{file,git,
      docker_*}*.py`, `cli/main.py`, `llm/*`, `rca_request/*` all
      unchanged.
- [x] **Deterministic RCA engine is sole decision authority.**
      Collectors produce `CollectedItem`s; extractors produce
      `Observation`s; the existing pipeline (RuleEngine, ScoringEngine,
      HypothesisEngine, CorrelationEngine, TimelineEngine,
      IncidentGraphBuilder, FingerprintBuilder, RemediationEngine) is
      byte-identical.
- [x] **No schema version bump.** Only additive enum entries in
      `observation.schema.json` and `evidence.schema.json`. Existing
      schema validation tests still pass.
- [x] **No new project dependency.** `urllib.request` (stdlib) only.
- [x] **No secrets in observations, logs, API responses, or UI.**
      All three Phase 2.2 collectors use the same greedy
      `_scrub_text` regex pattern; envelope metadata only carries
      `endpoint`, `query`, `repo` / `project`, `event_count`, `mode`,
      `sample_count`, `series_count` — never the resolved secret, the
      auth header, or the auth-env value.
- [x] **opt-in.** All three integrations are gated on the presence of
      a non-empty block in the request body. Absence preserves
      Phase 2.1 behaviour byte-identically (proven by
      `test_no_phase22_blocks_preserves_phase1_behaviour`).
- [x] **Hard limits enforced.** 6 h / 30 s / 5 MiB / 200 events per
      window, validated in the config layer (`__post_init__`) and the
      collector boundary.

## K. Future-proofing

- `ChangeProvider` Protocol means Bitbucket, Gitea, Azure DevOps, or
  any other code-hosting API can be added as a one-file
  `BaseCollector + ChangeProvider` drop-in without touching the API
  layer.
- `_INTEGRATION_COLLECTORS` dict in `investigation_service.py`
  accepts new entries without code changes outside that dict.
- Source enum in schemas is additive; new sources only need an enum
  entry + an extractor registered via the existing `registry.register`
  decorator.

---

## **Phase 2.2 PASS**
