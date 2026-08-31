# Phase 2.1 Report — Real Integration Foundation + Elasticsearch

Date: 2026-08-27
Branch: `autorca-port-conflict-incident`
Baseline: `python3 -m pytest -q` → 142 passed (Phase 1 green)

---

## TL;DR — Phase 2.1 PASS

- **175 tests pass** (142 baseline + 33 new). No Phase 1 test was modified.
- The deterministic RCA engine remains the sole decision authority.
- ES integration is opt-in: when no `elasticsearch` block is sent in
  the request body, behaviour is byte-identical to Phase 1.
- Live ES E2E was skipped (no local Elasticsearch reachable) per
  explicit user direction; the report logs that decision.

---

## A. Architecture changes

The integration pattern is a thin additive seam:

```
External Integration  (Elasticsearch, future Prometheus / K8s / GitHub / …)
        ↓
Collector Adapter     (collectors/elasticsearch_collector.py — stdlib urllib)
        ↓
CollectedItem         (collectors/base.py — unchanged)
        ↓
Normalised JSON envelope (source="elasticsearch", kind="generic_log_line")
        ↓
ElasticsearchExtractor (extractors/elasticsearch_extractor.py)
        ↓
Existing pipeline      (PipelineInput.sources → unchanged)
        ↓
Evidence → RuleEngine → Timeline → Correlation → Graph →
Fingerprint → Remediation → HypothesisAssessment
```

The integration layer adds **only** a new source key (`"elasticsearch"`)
and **reuses** the existing `generic_log_line` kind, so the schema
(`observation.schema.json`, `evidence.schema.json`) only required an
additive enum entry — no schema version bump.

---

## B. Files created

| Path | Purpose |
|---|---|
| `collectors/integration_base.py` | `IntegrationConfig`, `IntegrationError`, `IntegrationResult`, hard caps |
| `collectors/elasticsearch_collector.py` | `ElasticsearchCollector` (stdlib `urllib.request`) + envelope builder + safe hit projection + secret-safe header builder |
| `extractors/elasticsearch_extractor.py` | Envelope → `Observation(kind="generic_log_line", source="elasticsearch")` |
| `tests/test_phase2_elasticsearch_collector.py` | 26 unit tests |
| `tests/test_phase2_api_extension.py` | 7 HTTP-level integration tests |
| `docs/PHASE2_1_REPORT.md` | this document |

## C. Files modified (additive only)

| File | Change |
|---|---|
| `extractors/registry.py` | append `"elasticsearch"` to `VALID_SOURCES` tuple |
| `schemas/observation.schema.json` | append `"elasticsearch"` to `source` enum |
| `schemas/evidence.schema.json` | append `"elasticsearch"` to `source` enum |
| `pipeline.py` | one new `import extractors.elasticsearch_extractor  # noqa: F401` for side-effect registration |
| `api/security.py` | new `validate_integration_url(name, url, allow_loopback=None)` helper |
| `api/investigation_service.py` | new `_run_elasticsearch_collection()` helper + 1 conditional branch in `create_investigation` |
| `api/serializers.py` | new `elasticsearch: Optional[Dict]` keyword on `investigation_payload` |
| `reporting/incident_report_renderer.py` | add `elasticsearch` (and Phase 1 sources) to `_SOURCE_LABELS` dictionary |
| `web/static/index.html` | append 4 optional form inputs under the existing form (URL, index, service, size) |
| `web/static/app.js` | append a 12-line block that builds the `elasticsearch` payload from the form |

## D. Files intentionally untouched

- `engine/rule_engine.py`, `engine/scoring_engine.py`,
  `engine/hypothesis_engine.py`, `engine/correlation_engine.py`,
  `engine/timeline_engine.py`, `engine/incident_graph.py`,
  `engine/incident_fingerprint.py`, `engine/remediation_engine.py`
- `evidence/evidence_builder.py`, `validation/final_rca_validator.py`
- `taxonomy/taxonomy.yaml`, `rules/rules.config.json`,
  `config/rules_config.py`
- `cli/main.py` (existing argument set unchanged)
- All Phase 1 collectors (`collectors/{file,git,docker_*,host_metrics}_*.py`)
- `collectors/base.py` (existing `IncidentContext` and `CollectedItem`
  are reused, not duplicated)
- All Phase 1 extractors
- `extractors/base.py` (`Observation` and `BaseExtractor.build_observation`
  reused unchanged)
- `llm/*`, `rca_request/*`
- `schemas/rca_request.schema.json`, `schemas/final_rca.schema.json`,
  `schemas/hypothesis.schema.json`

---

## E. New tests

`tests/test_phase2_elasticsearch_collector.py` (26 tests):

1. `test_successful_collection_returns_one_collected_item`
2. `test_time_window_is_applied_to_query_body`
3. `test_service_filter_is_emitted_when_configured`
4. `test_size_is_clamped_to_max`
5. `test_timeout_raises_integration_error`
6. `test_http_401_raises_integration_error`
7. `test_http_500_raises_integration_error`
8. `test_malformed_json_raises_integration_error`
9. `test_basic_auth_header_is_sent_when_env_set`
10. `test_secret_value_does_not_leak_into_items`
11. `test_auth_env_name_scrubbed_from_integration_metadata`
12. `test_unsupported_url_scheme_rejected`
13. `test_embedded_userinfo_rejected`
14. `test_oversized_response_raises_integration_error`
15. `test_connection_refused_raises_integration_error`
16. `test_dns_failure_raises_integration_error`
17. `test_extractor_produces_observations_of_generic_log_line`
18. `test_extractor_handles_malformed_envelope`
19. `test_empty_es_response_does_not_crash`
20. `test_phase1_collector_still_importable`
21. `test_registry_includes_elasticsearch`
22. `test_pipeline_registers_elasticsearch_extractor`
23. `test_validate_integration_url_rejects_loopback_by_default`
24. `test_validate_integration_url_accepts_loopback_when_allowed`
25. `test_validate_integration_url_rejects_missing_host`
26. `test_incident_context_fills_missing_window`
27. `test_unbounded_window_rejected`

`tests/test_phase2_api_extension.py` (7 tests):

1. `test_elasticsearch_block_runs_collector_and_surfaces_in_payload`
2. `test_no_elasticsearch_block_preserves_phase1_behaviour`
3. `test_unsupported_scheme_returns_400`
4. `test_missing_index_pattern_returns_400`
5. `test_integration_failure_surfaces_as_400`
6. `test_extra_headers_cannot_override_authorization`

## F. Total test count

| Suite | Before Phase 2.1 | After Phase 2.1 |
|---|---|---|
| `pytest -q` (full suite) | 142 | **175** |
| New Phase 2.1 tests | — | 33 |

## G. Baseline comparison

- Baseline (Phase 1): **142 passed in 18.43s**.
- Phase 2.1 final: **175 passed in 22.55s**.
- Δ = +33 tests, +4.12s. Zero Phase 1 tests modified.
- All `tests/test_api_investigations.py`, `tests/test_ui_console.py`,
  `tests/test_schema_validation.py`, `tests/test_collectors_and_cli.py`
  pass unchanged.

---

## H. Security controls

| Control | Implementation |
|---|---|
| Scheme allowlist | `validate_integration_url` rejects anything that isn't `http`/`https` |
| Embedded credentials | `validate_integration_url` rejects URLs with `userinfo` (`https://user:pass@...`) |
| Auth source | credentials come **only** from an env-var name; the secret is read inside the collector and never passed in by the API caller |
| Auth secret masking | the collector scrubs Authorization from any log/exception path via `_scrub_text`; `extra_headers` cannot override Authorization (rejected at API boundary) |
| SSRF (loopback / private / link-local) | blocked by default; `AUTORCA_ES_ALLOW_LOOPBACK=1` opt-in for tests |
| Request timeout | `urllib.request.urlopen(..., timeout=...)` + `socket.setdefaulttimeout`; clamped `[0.5, 30]s` |
| Response size cap | 5 MiB streamed read; `IntegrationError` if exceeded |
| Result count cap | `size` clamped to `[1, 1000]` |
| Unbounded query | rejected — at least one of `incident_start`/`incident_end` required |
| No subprocess, no shell | `urllib.request` only |
| Loud failure | `IntegrationError → AnalysisRequestError → HTTP 400`; never returns empty silently |
| Body sanitisation | raw ES documents are projected to a small whitelist (`index`, `id`, `timestamp`, `message`, `service`, `level`, `host`); nothing else reaches the pipeline |

---

## I. Elasticsearch integration behaviour

- **Query body**: deterministic, bounded `POST /{index_pattern}/_search`
  with `_source` restricted to `@timestamp / message / service /
  log.level / host.name`, `sort: @timestamp desc`, `size` clamped.
- **Time window**: `incident_start`/`incident_end` produce a
  `range` filter on `@timestamp`. The collector refuses to query when
  both bounds are missing.
- **Service filter**: optional; added as a `term` filter on `service`
  when configured.
- **Custom query fragment**: optional; when present it must be a JSON
  object with a `query` key, and is composed with the time filters
  inside a `bool.must`/`bool.filter` envelope.
- **Adapter output**: a single `CollectedItem(source="elasticsearch",
  raw_text=JSON_envelope, metadata={hit_count, index_pattern, ...})`.
  The envelope includes the time window, service filter, and a
  projected list of safe hits — never the raw ES document verbatim.
- **Normalization**: `ElasticsearchExtractor` parses the envelope and
  emits one `Observation(kind="generic_log_line", source="elasticsearch",
  service=..., resource=f"elasticsearch:{index_pattern}", …)` per
  safe hit, using the existing V2 fields.
- **Failure semantics**: timeout, HTTP 4xx/5xx, malformed JSON,
  oversized body, connection refused, DNS failure → `IntegrationError`
  → `AnalysisRequestError` → HTTP 400 with a redacted message.
- **AutoRCA without Elasticsearch**: works identically to Phase 1.
  `is_available()` is never queried by the deterministic pipeline
  (the integration is API-driven); the absence of the
  `elasticsearch` block in the request leaves `sources` untouched.

---

## J. API changes

**Endpoint:** `POST /api/v1/investigations` (unchanged path/method).

**Request body:** optionally adds an `elasticsearch` object:

```json
{
  "repo": "/path/to/repo",
  "environment": "production",
  "elasticsearch": {
    "url": "https://es.example.com:9200",
    "index_pattern": "logs-*",
    "service": "payment-api",
    "incident_start": "2026-08-27T10:00:00Z",
    "incident_end":   "2026-08-27T11:00:00Z",
    "size": 100,
    "timeout_seconds": 5.0,
    "auth_env": "AUTORCA_ES_BASIC_AUTH",
    "verify_tls": true,
    "extra_headers": { "X-Scope": "production" }
  }
}
```

All keys are optional except `url` and `index_pattern`. When omitted,
behaviour is byte-identical to Phase 1.

**Response payload:** adds an `elasticsearch` block summarizing the
collector run (endpoint, index pattern, service, time window, size,
hit count, HTTP status). Never contains the secret, never contains
the auth env-var name.

**No new endpoints.** Existing endpoints behave unchanged:
`GET /api/v1/investigations`, `GET /api/v1/investigations/{id}`,
`GET /api/v1/investigations/{id}/{section}` all work as before.

---

## K. UI changes

- Optional fields added to the existing form under "REAL INCIDENT INPUT":
  - `Elasticsearch URL` (http/https only)
  - `Elasticsearch index pattern` (`logs-*`)
  - `Elasticsearch service filter` (`payment-api`)
  - `Elasticsearch size` (`1`–`1000`)
- `app.js` only forwards an `elasticsearch` block when the URL is set,
  preserving the exact Phase 1 payload shape otherwise.
- ES observations surface automatically in the existing Evidence tab
  (each row already prints `Source: ${obs.source}`).
- No new tabs, no removed IDs, no `app.js` API changes.
- `tests/test_ui_console.py` was not modified — all UI markers
  (`#new-investigation-panel`, `#incidents-panel`, etc.) remain
  intact and the existing tests pass unchanged.

---

## L. Real E2E result

**Live ES E2E: SKIPPED — no local Elasticsearch reachable.**

`curl -s -m 1 http://127.0.0.1:9200` returns an empty body; no local
ES process was running. Per the user's explicit pre-implementation
choice, no fake-ES harness was added either. The 33 unit tests
exercise every code path with `urllib.request` mocked — including
auth, time-window, service filter, oversized body, malformed JSON,
connection refused, DNS failure, HTTP 401/500, and end-to-end
collector → extractor → `Observation` round-trip.

If a real ES becomes available, the same `ElasticsearchCollector` is
ready to be exercised end-to-end; no code changes are needed.

---

## M. Known limitations

- **DNS-based SSRF not blocked at the API boundary**: literal IP
  addresses that resolve to private ranges are blocked, but a
  hostname that resolves to a private IP at request time would not
  be caught by the literal-IP check. The Architecture Freeze §8
  documents that DNS-based SSRF is mitigated by TLS + the network
  egress policy, not by the URL validator.
- **No retry/backoff**: a single ES call attempt; failures bubble up
  as `IntegrationError`. Retry/backoff is deferred to Phase 2.2+.
- **No streaming/incremental ingestion**: results are returned in one
  HTTP call bounded by `size` (≤1000). Multi-page scroll is not
  implemented.
- **No support for ES SQL / ES DSL aggregates**: the collector uses
  the deterministic `_search` query shape with optional user
  `query` JSON fragment composed inside `bool.must`.
- **The collector does not validate that the configured endpoint is
  actually an Elasticsearch cluster**: a misconfigured URL pointing
  at, say, a Prometheus server would fail at the JSON-parse step
  with a clear `IntegrationError`.
- **`api/serializers.serialise_observation` does not include the V2
  `service`/`resource` fields** in the API response; this is a
  Phase 1 artifact, not a Phase 2.1 regression. The data is still
  propagated into the underlying `Evidence` record (which the
  UI consumes).

---

## N. Technical debt

- **TD-201**: `_safe_read` is a duplicate of the streaming helper
  already living (in spirit) in `web_app.py`. Could be lifted into a
  shared `api/safe_io.py` module later.
- **TD-202**: The ES envelope carries `index_pattern` and `service`
  at the envelope level AND inside each hit record. Slightly
  redundant; can be collapsed in Phase 2.2 if a second integration
  reuses the same shape.
- **TD-203**: `_redact_headers()` is not a helper yet — secret
  scrubbing is currently inline (`_scrub_text`). Once a second
  integration appears, lift it.
- **TD-204**: The `validate_integration_url` allow-list is hard-coded
  to a small set of well-known ranges. Future integrations that need
  to whitelist by ASN or by specific hostname will need a config-
  driven extension.

---

## O. Exact commands used

```bash
# Baseline verification
python3 -m pytest -q
# → 142 passed in 18.43s

# Full Phase 2.1 verification
python3 -m pytest -q
# → 175 passed in 22.55s

# CLI regression
python3 -m cli.main analyze --help
# (prints unchanged argument list)

# Schema validation regression
python3 -m pytest tests/test_schema_validation.py -q
# → 9 passed

# UI / CLI / API regression
python3 -m pytest tests/test_ui_console.py tests/test_collectors_and_cli.py \
                  tests/test_schema_validation.py tests/test_api_investigations.py -q
# → 50 passed

# Phase 2.1 tests only
python3 -m pytest tests/test_phase2_elasticsearch_collector.py \
                  tests/test_phase2_api_extension.py -q
# → 33 passed

# Live ES probe
curl -s -m 1 http://127.0.0.1:9200
# → empty (no local ES) → live E2E skipped
```

---

## P. Git status (final)

```
 M api/investigation_service.py
 M api/security.py
 M api/serializers.py
 M extractors/registry.py
 M pipeline.py
 M reporting/incident_report_renderer.py
 M schemas/evidence.schema.json
 M schemas/observation.schema.json
 M web/static/app.js
 M web/static/index.html
?? collectors/integration_base.py
?? collectors/elasticsearch_collector.py
?? docs/PHASE2_1_REPORT.md
?? extractors/elasticsearch_extractor.py
?? tests/test_phase2_api_extension.py
?? tests/test_phase2_elasticsearch_collector.py
```

(The other entries in `git status` are Phase 1 deliverables already
in the working tree.)

---

## Verdict

**Phase 2.1 — PASS.**

- 142 baseline tests preserved.
- 33 new tests added; all 175 tests pass.
- No protected module (`engine/*`, `evidence/evidence_builder.py`,
  `validation/*`, `taxonomy/*`, `rules/*`, `cli/main.py`,
  `llm/*`, `rca_request/*`, existing extractors, existing collectors)
  was modified in a way that changes behaviour.
- CLI, API, UI, and schema-validation regression tests all pass.
- Security controls cover scheme allowlist, embedded credentials,
  basic-auth via env var, secret scrubbing, SSRF for literal IPs,
  timeout, response size cap, and loud failure.
- Live ES E2E explicitly skipped with rationale logged; ready to
  re-run against any reachable cluster without code changes.

Do not continue to Phase 2.2 until the user accepts this report.
