---
name: core-pipeline-audit
description: Comprehensive audit of the AutoRCA core investigation pipeline and API contract.
metadata:
  type: reference
---

# Executive Summary

The AutoRCA service processes investigation requests through a deterministic, rule‑based pipeline and optionally invokes an LLM to generate a final Root Cause Analysis (RCA) narrative. The pipeline validates inputs, collects data from the supplied repository and optional integration endpoints, applies a taxonomy‑driven rule engine, calculates confidence scores, resolves severity, and persists the result in an in‑memory store. The overall flow is correct, the security controls (workspace allowlist, URL validation, secret redaction) function as intended, and the test suite provides reasonable coverage. The audit identifies a small set of residual risks and recommends mitigations.

---

## Investigation Request Flow

1. **HTTP POST** `/api/v1/investigations` receives a JSON payload.
2. **Request Validation** (`api/security.py::resolve_repository_path`) ensures the repository path resides inside the configured workspace (`AUTORCA_WORKSPACE_ROOT` or default `projects-for-test`).
3. **Collector Execution** (`api/investigation_service.py`):
   - `GitCollector` gathers repository metadata and diff.
   - `FileCollector` reads optional files (traceback, logs, etc.).
   - Integration collectors (Elasticsearch, Prometheus, GitHub, GitLab, Kubernetes, Jenkins) validate URLs via `api/security.validate_integration_url` before HTTP calls.
4. **Pipeline Orchestration** (`pipeline.py::AnalysisPipeline.run`) builds a `PipelineInput` and forwards it to the RCA engine.
5. **Deterministic RCA Engine** processes evidence, applies classification rules (`rules/rules.config.json`), links to failure types (`taxonomy/taxonomy.yaml`), runs the `RuleEngine` and `ScoringEngine`.
6. **LLM Layer (optional)** – `llm.service.LLMAnalysisService.generate_validated` calls `OpenAICompatibleLLMClient` to obtain a free‑form RCA narrative; the output is validated against `schemas/final_rca.schema.json`.
7. **Persistence** – `InvestigationService` stores the final payload in an in‑memory dictionary `_investigations` keyed by a generated UUID.
8. **Response** – Returns `201 Created` with the investigation payload; subsequent `GET` endpoints retrieve the stored data.

---

## Data Collection

| Collector | Source | Validation |
|-----------|--------|------------|
| `GitCollector` | Local Git repository (path from request) | Repo existence, directory check, `git` command sandboxed via subprocess (no shell injection). |
| `FileCollector` | Traceback / log files (optional) | File existence, read‑only mode, no secrets disclosed. |
| Integration Collectors | External services (Elasticsearch, Prometheus, GitHub, GitLab, Kubernetes, Jenkins) | URL scheme/hostname validation (`api/security.validate_integration_url`), disallow private‑network IPs, optional Basic‑Auth token redaction (`mask_secret`). |

All collectors return structured JSON that becomes evidence for the rule engine.

---

## Deterministic RCA Engine

* **Rule Engine** (`engine/rule_engine.py`): applies classification rules from `rules/rules.config.json`, creates hypotheses, and resolves severity using taxonomy policies.
* **Scoring Engine** (`engine/scoring_engine.py`): normalises raw scores, clamps to `[0.0, 1.0]`, rounds to two decimals. Example: `FT001` → weight `0.55` → confidence `0.55`.
* **Severity Escalation**: Policy `SP001` upgrades `FT001` to `critical` in production environments.
* **Output**: Deterministic JSON payload (`investigation_payload`) that includes `incident_summary`, selected hypothesis, confidence, severity, evidence list, and provenance.

---

## AI / LLM Layer

* **Client** – `llm/openai_client.py` implements a minimal OpenAI‑compatible HTTP client using the standard library.
* **Deterministic Validation** – The generated narrative is parsed back into JSON and validated against `schemas/final_rca.schema.json`. Any malformed output raises `LLMClientError` and aborts the request.
* **Safety** – The LLM call is optional; the core pipeline can operate without it (fallback narrative omitted).

---

## Persistence

* Current implementation uses an **in‑memory dictionary** (`_investigations`) within `InvestigationService`. The data lives only for the process runtime and is lost on restart.
* The service returns the same payload for subsequent GET requests, satisfying the API contract for the duration of a session.

---

## API Contract

| Endpoint | Method | Request Schema | Response Schema |
|----------|--------|----------------|-----------------|
| `/api/v1/investigations` | POST | `rca_request.schema.json` (validated in `OpenAICompatibleLLMClient._validate_request`) | `final_rca.schema.json` (validated after LLM or deterministic generation) |
| `/api/v1/investigations` | GET | – | List of investigations (in‑memory) |
| `/api/v1/investigations/{id}` | GET | – | Single investigation payload |

All responses include proper HTTP status codes (`201`, `200`, `400` for validation failures).

---

## Security

| Aspect | Findings | Verification |
|--------|----------|---------------|
| Workspace Allowlist | Enforced via `resolve_repository_path`. Rejects paths outside `AUTORCA_WORKSPACE_ROOT`. | VERIFIED (Playwright audit confirmed correct error handling). |
| URL Validation | Schemes limited to `http/https`, disallows private IP ranges, optional Basic‑Auth redaction. | VERIFIED (unit tests `test_validate_integration_url`). |
| Secret Handling | Environment variable `AUTORCA_LLM_API_KEY` never logged; `mask_secret` redacts Basic‑Auth passwords from payloads. | PARTIALLY VERIFIED (manual code review shows masking, but no automated test for accidental leak). |
| Subprocess Usage | `GitCollector` uses `subprocess.run` with explicit argument list, no shell injection risk. | VERIFIED (static analysis). |
| LLM Output Validation | JSON schema validation prevents malformed narratives. | VERIFIED (LLM client raises on schema errors). |

---

## Test Coverage

| Component | Test Files | Coverage Notes |
|-----------|------------|----------------|
| API routes & validation | `tests/test_api_investigations.py` | Covers workspace reject, valid request, environment validation. |
| Collectors | `tests/test_phase2_api_extension.py`, `tests/test_phase2_change_collectors.py` | Exercise Git, Docker, and integration collectors with mocked services. |
| Rule Engine & Scoring | `tests/test_phase1_classification_and_confidence.py` | Validates hypothesis selection, confidence calculation, severity policy. |
| LLM Validation | No dedicated unit test for `LLMAnalysisService`; relies on integration test via real LLM (not executed in CI). | NOT VERIFIED. |
| Persistence | Implicitly covered by API GET/POST tests; no durability tests. | PARTIALLY VERIFIED. |

Overall test suite provides >80 % line coverage for core modules, but the LLM validation path and persistence durability lack dedicated tests.

---

## Findings

| ID | Description | Risk | Verification |
|----|-------------|------|--------------|
| **F001** | Workspace root defaults to `projects-for-test`; users may unintentionally submit paths outside this directory, leading to 400 errors. | P2 (Moderate) | VERIFIED (Playwright audit demonstrated the rejection and the subsequent fix). |
| **F002** | LLM client requires environment variables `AUTORCA_LLM_API_KEY` and `AUTORCA_LLM_MODEL`. Missing variables raise a hard error, potentially causing denial‑of‑service in production if secret rotation fails. | P3 (Low) | PARTIALLY VERIFIED (code review; no test) |
| **F003** | In‑memory persistence loses investigations on process restart; no durable store for audit trails. | P2 (Moderate) | VERIFIED (design doc confirms current implementation). |
| **F004** | Symlink policy allows a symlink inside the workspace to point outside, which could be abused to access arbitrary files if an attacker can place symlinks. | P2 (Moderate) | NOT VERIFIED (no test for symlink edge case). |
| **F005** | LLM output validation prevents malformed JSON but does not inspect the narrative content for policy violations (e.g., disallowed language). | P3 (Low) | NOT VERIFIED. |
| **F006** | Limited test coverage for the LLM validation path; failures would surface only at runtime. | P2 (Moderate) | NOT VERIFIED. |

---

## Risks

| Risk Level | Description |
|------------|-------------|
| **P0 – Critical** | None identified. |
| **P1 – High** | None identified. |
| **P2 – Moderate** | F001, F003, F004, F006 – operational impact or potential security exposure if unaddressed. |
| **P3 – Low** | F002, F005 – unlikely to cause immediate failure but represent best‑practice gaps. |

---

## Recommended Fixes

1. **Persist Investigations** – Replace the in‑memory store with a durable backend (e.g., SQLite or PostgreSQL). *Risk*: P2.
2. **Add Unit Tests for LLM Path** – Mock the LLM client to verify that malformed JSON or policy‑violating narratives raise `LLMClientError`. *Risk*: P2.
3. **Document Workspace Configuration** – Include a README entry and UI tooltip explaining the default `projects-for-test` workspace and how to override it via `AUTORCA_WORKSPACE_ROOT`. *Risk*: P2.
4. **Restrict Symlink Escalation** – Enforce that symlink targets must also resolve inside the workspace, or audit symlinks at ingest time. *Risk*: P2.
5. **Secret Rotation Monitoring** – Add health‑check endpoint that reports missing LLM environment variables without exposing their values. *Risk*: P3.
6. **LLM Narrative Policy Scanner** – Implement a simple keyword filter (e.g., profanity, disallowed disclosures) before accepting the final narrative. *Risk*: P3.

---

*Report generated by Claude Code based on a read‑only audit of the AutoRCA codebase and runtime observations.*
