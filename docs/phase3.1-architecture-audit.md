---
name: phase3.1-architecture-audit
description: Architecture audit for Phase 3.1 – persistence & domain model transition (revised).
metadata:
  type: reference
---

# Phase 3.1 Architecture Audit (Revised)

**Purpose** – Before any code changes we must capture the exact current state of AutoRCA, clarify the persistence requirements, and define a production‑ready PostgreSQL boundary that **does not silently fall back to an in‑memory store**. This document replaces the previous draft and incorporates the mandatory constraints supplied by the product owner.

---

## 1. Current Persistence Behaviour (unchanged)

| Aspect | Detail |
|--------|--------|
| **Storage location** | `api/investigation_service.py` – a module‑level dict `_investigations: Dict[str, Investigation]` kept in the `InvestigationService` instance. |
| **Lifetime** | Process‑local. Data disappears when the Python process exits or the server restarts. |
| **Creation** | `InvestigationService.create_investigation` builds an `Investigation` dataclass (lines 97‑355) and stores it in `_investigations` under a generated UUID (`INV‑…`). |
| **Read** | `list_investigations`, `get_investigation` return the stored `Investigation` objects (or their `payload`). |
| **Update** | Only status/elapsed time are set during creation; there is no later mutation of the payload. |
| **Transactionality** | None – the whole investigation result is built synchronously, then inserted into the dict. |
| **Idempotency** | Investigation IDs are generated with `uuid.uuid4()`; repeated POSTs produce new investigations. |
| **Security** | No secrets are stored; `api/security` masks credentials before they ever reach the payload. |
| **Tests** | `tests/test_api_investigations.py`, `tests/test_real_scenarios.py` verify that the endpoint returns a deterministic payload and that the payload can be retrieved later in the same process. |

---

## 2. Current Domain Objects (unchanged)

| Module | Class / Dataclass | Primary Fields | Usage |
|--------|-------------------|----------------|------|
| `api/investigation_service.py` | `Investigation` (lines 68‑84) | `investigation_id`, `status`, `created_at`, `duration_ms`, `repository`, `repository_full_name`, `environment`, `branch`, `commit_sha`, `payload`, `error`, `inputs` | Represents a completed investigation as stored in the in‑memory dict. |
| `api/investigation_service.py` | `InvestigationService` | `_investigations` dict, `_pipeline` reference | Service layer that validates the request, runs collectors, runs `AnalysisPipeline`, builds the JSON payload via `api/serializers.investigation_payload`, and persists the `Investigation`. |
| `pipeline.py` | `PipelineResult` | `analysis_id`, `observations`, `evidence_list`, `hypotheses`, `timeline`, `correlation`, `graph`, `fingerprint`, `remediation`, `hypothesis_assessment` | Output of the deterministic engine; later transformed into the API payload. |
| `engine/*` | Various domain entities (`Hypothesis`, `Observation`, `IncidentTimeline`, `CorrelationGraph`, `IncidentGraph`, `IncidentFingerprint`, `RemediationContext`) | Engine‑specific data structures, mostly immutable during a run. | Consumed by `pipeline.run` and serialized by `api/serializers`. |
| `api/serializers.py` | Helper functions (`serialise_observation`, `serialise_evidence`, …) | Convert engine objects to JSON‑serialisable dicts; `investigation_payload` assembles the final response dict. |

_No ORM or database models exist yet; all data lives as plain Python objects._

---

## 3. Current API → Service → Pipeline Flow (unchanged)

```
POST /api/v1/investigations
    └─ AutoRCAHandler.do_POST (web_app.py)
          reads JSON payload, validates size, parses JSON
          │
          └─ _get_investigation_service() → singleton InvestigationService
                └─ InvestigationService.create_investigation(request)
                      ├─ validate_request / resolve_repository_path / validate_environment
                      ├─ Run collectors (Git, File, optional integrations)
                      ├─ Build sources dict (git_diff, traceback, …)
                      ├─ Build PipelineInput (analysis_id, sources, env)
                      ├─ pipeline.run → PipelineResult (deterministic engine)
                      ├─ Compute confidence / severity
                      ├─ Build deterministic payload via investigation_payload()
                      └─ Store Investigation dataclass in in‑memory dict
                         └─ Return payload → HTTP 201 response
```

All UI routes (`/api/v1/investigations/{id}` and its sub‑paths) simply fetch the stored `Investigation` and serve the JSON sections stored in `payload`.

---

## 4. In‑Memory Store (unchanged) – reference only for tests/local mode

- Plain dictionary `_investigations: Dict[str, Investigation]`.
- Thread‑safe via `threading.Lock`.
- No persistence across restarts.
- Used by the existing test suite and by developers when `AUTORCA_PERSISTENCE=memory`.

---

## 5. Objects that Must Be Persisted (production requirement)

To reconstruct the UI **without re‑running the deterministic engine**, the persisted representation must contain **all** data that the UI reads via the various sub‑endpoints. The minimal set is:

1. **Investigation core metadata** – ID, organization_id, project_id, repository information, environment, branch, commit SHA, status, created_at, completed_at (duration_ms), confidence, severity, root‑cause identifiers.
2. **Full deterministic payload** – The exact JSON dict produced by `investigation_payload`. Storing this payload as a **JSONB column** (`payload`) gives us a single source of truth for UI rendering and avoids duplication of the same information in separate tables.
3. **Optional indexed child tables** – For use‑cases that need efficient querying (e.g., filtering investigations by failure type) we may expose lightweight read‑only tables derived from the payload:
   - `observations` (id, kind, source, location, data, extracted_at, investigation_id)
   - `evidence` (id, failure_type_id, summary, raw_reference, investigation_id)
   - `hypotheses` (id, label, score, status, investigation_id)
   These tables **do not store duplicate JSON**; they contain only the fields required for indexing/searching and reference back to the parent investigation via a foreign key. The UI continues to read the full payload from the `payload` column; the child tables are purely an optimisation.
4. **Organization & Project** – Explicit tables with a one‑to‑many relationship to investigations. For the current Phase 3.1 we can seed a single organization and project row; the schema nevertheless supports multi‑tenant expansion in later phases.

---

## 6. Proposed PostgreSQL Boundary (updated)

```
InvestigationService
        │
        ▼
InvestigationRepository (protocol)
        │
   ┌────┴─────┐
   │          │
   ▼          ▼
InMemoryRepo  PostgresRepo (SQLAlchemy + Alembic)
```

- **Repository protocol** defines `create(investigation: InvestigationDomain) → InvestigationDomain`, `get(id)`, `list()`, `update_status(id, new_status)`, and `persist_full_result(id, payload_json)`.
- **In‑memory implementation** – unchanged; used only when `AUTORCA_PERSISTENCE=memory` (explicit for tests or local development).
- **PostgreSQL implementation** – **no silent fallback**. If the environment variable `AUTORCA_PERSISTENCE=postgres` is set, the server attempts to create a SQLAlchemy engine with `AUTORCA_DATABASE_URL`. **Any connection error or migration failure aborts startup** with a clear exception; the process exits. This guarantees that production cannot accidentally revert to the in‑memory store.
- **Dependency injection** – `_get_investigation_service` reads `AUTORCA_PERSISTENCE` and constructs the appropriate repository. The deterministic RCA engine (`AnalysisPipeline`) remains untouched.

---

## 7. Migration Strategy (re‑specified)

1. **Add repository abstraction** – Create `persistence/repositories/investigation_repository.py` containing the protocol and two concrete classes (`InMemoryInvestigationRepository`, `PostgresInvestigationRepository`). Update `InvestigationService` to accept an instance of this protocol.
2. **Define ORM models** – `persistence/models/` with:
   - `organization` (id PK, name)
   - `project` (id PK, organization_id FK, name)
   - `investigation` (id PK, organization_id FK, project_id FK, repository, repository_full_name, environment, branch, commit_sha, status, created_at, completed_at, confidence, severity, payload JSONB, optional `idempotency_key` UNIQUE)
   - `observation`, `evidence`, `hypothesis` tables as **lean indexes** (optional; can be added in a later migration). They reference `investigation.id`.
3. **Alembic migration `0001_initial.py`** creates the tables listed above, sets appropriate foreign‑key constraints, and adds a unique index on `investigation.idempotency_key` (if the column is used).
4. **Configuration flag** – `AUTORCA_PERSISTENCE=memory|postgres`. Default **memory** to keep the existing test suite green. For production the deployment must set `postgres` and provide a valid `AUTORCA_DATABASE_URL`.
5. **No silent fallback** – The server startup code checks the flag; when `postgres` is selected, it attempts to connect and run pending Alembic migrations. If any step fails, the exception is logged and the process exits with a non‑zero status code.
6. **Data migration** – Because the existing system has no persisted data, the first migration simply creates an empty schema. Future migrations can add columns or child tables without data loss.

---

## 8. Backward Compatibility (updated)

- **API contract** – The JSON structure returned by all endpoints remains *exactly* the same because `InvestigationService` continues to call `investigation_payload` and stores the resulting dict in the `payload` JSONB column. Sub‑endpoint handlers read the relevant slice (`payload->'observations'`, etc.) directly from the column or, if child tables are present, they reconstruct the same dict to guarantee identical output.
- **CLI** – Unchanged; it consumes the same HTTP endpoints.
- **Tests** – Existing 292 tests continue to use the default `memory` mode and therefore run unchanged. New integration tests will explicitly set `AUTORCA_PERSISTENCE=postgres`.
- **Feature flag** – Switching between modes is an explicit configuration change; there is **no automatic fallback**.

---

## 9. Idempotency (clarified)

- **Simple request‑level idempotency** – Clients may optionally include an HTTP header `Idempotency-Key` (a UUID‑v4 string). The repository stores this key in a unique column on the `investigation` table. When a creation request arrives with a key that already exists, the service returns the existing investigation payload with status `200 OK` instead of creating a duplicate record.
- **No distributed coordination** – This mechanism works within a single instance (or a cluster sharing the same database) and satisfies the Phase 3.1 requirement without introducing a heavyweight distributed idempotency service.
- **If the header is omitted**, the request behaves as before (a new investigation is always created).

---

## 10. Failure / Recovery Behaviour (updated)

- **Transactional persistence** – All inserts for a single investigation (core row + any optional child rows) occur inside a single SQLAlchemy transaction. On any error the transaction is rolled back, leaving **no partial investigation**.
- **Integration collector failures** – They are handled *before* persistence (as in the current code). The resulting failure information is stored in the `payload.integrations` JSON field; the transaction still commits because the payload remains valid.
- **Service start‑up failure** – When `AUTORCA_PERSISTENCE=postgres` and the DB is unreachable or migrations cannot be applied, the server raises an exception and stops. This meets the **no silent fallback** rule.
- **Restart recovery** – Because investigations are fully persisted, a server restart simply creates a new `InvestigationService` (which will read from the DB when needed). No in‑memory cache is required for completed investigations.
- **Idempotency handling** – Duplicate `Idempotency-Key` submissions return the existing investigation without creating a new row.

---

## 11. Test Strategy (updated)

### Unit / Repository Tests
- Verify `PostgresInvestigationRepository.create` writes the core row and optional child rows, returns a domain object matching the payload.
- Verify `list` orders by `created_at` descending.
- Verify `get` reconstructs the exact payload stored in the `payload` column.
- Verify that a duplicate `idempotency_key` results in a `IntegrityError` that the service translates into a `200 OK` with the existing payload.
- Verify transaction rollback when an artificial failure is injected after the core row insert.

### Integration / E2E Tests (real PostgreSQL)
- Spin up a disposable PostgreSQL container (Docker) in the test harness (`pytest-docker` or a custom fixture).
- Set `AUTORCA_PERSISTENCE=postgres` and `AUTORCA_DATABASE_URL=postgresql://…`.
- Execute the full API flow: `POST /api/v1/investigations` → `GET /api/v1/investigations/{id}` → sub‑endpoint checks. Assert that the JSON responses are **byte‑for‑byte identical** to those produced by the in‑memory version (use the same deterministic payload).
- Restart the server process (create a new `InvestigationService` instance) and confirm that the previously created investigation is still retrievable.
- Test idempotency by sending the same request twice with the same `Idempotency-Key` header and verifying the second response returns the same `investigation_id` and payload.

### Regression Tests
- All existing 292 tests must remain green (they run under the default `memory` mode).
- Add a thin wrapper test suite that toggles the flag to `postgres` and runs a representative subset of the API tests to guarantee compatibility.

### Security Tests
- Ensure that the persisted `payload` JSON does **not** contain any raw secrets (e.g., values from environment variables). Use the same masking logic as `api/serializers._maybe_mask_observation_data`.
- Verify that the `idempotency_key` column is **not** exposed in any API response.

---

## 12. Summary of High‑Level Changes Required

1. **Create `persistence/` package** with:
   - `database.py` – SQLAlchemy engine creation, session factory, migration runner.
   - `models/` – ORM definitions for `Organization`, `Project`, `Investigation` (with `payload JSONB`), optional child tables.
   - `repositories/` – Protocol definition and the two concrete implementations (`InMemoryInvestigationRepository`, `PostgresInvestigationRepository`).
2. **Add configuration handling**:
   - New env vars: `AUTORCA_PERSISTENCE` (default `memory`), `AUTORCA_DATABASE_URL` (required when `postgres`).
   - Update `web_app._get_investigation_service` to construct the appropriate repository and abort on DB errors when `postgres` is selected.
3. **Refactor `InvestigationService`** to depend on the repository protocol rather than an internal dict. The public API of the service stays the same; only the persistence layer changes.
4. **Write Alembic migration `0001_initial.py`** that creates the schema described in Section 6.
5. **Update `pyproject.toml` / `requirements.txt`** to include `SQLAlchemy>=2.0`, `alembic`, and `psycopg2-binary` (or `psycopg2` if the environment already provides it).
6. **Add new tests** covering the PostgreSQL repository, idempotency, transactional rollback, and the full API flow against a real PostgreSQL container.
7. **Documentation** – Keep `docs/phase3.1-architecture-audit.md` (this revised version) and later produce a completion report as required.

---

## 13. Risks & Open Questions (still pending)

| Risk | Impact | Mitigation |
|------|--------|------------|
| **DB connection failure in production** – If the environment variable is mis‑configured, the service will abort (as required). Operators must ensure proper configuration; add a start‑up health‑check script. |
| **JSONB payload size** – Storing the full deterministic payload may approach the PostgreSQL row size limit for extremely large investigations. Mitigation: monitor payload size; if needed, split large sections into auxiliary tables (already modelled as optional child tables). |
| **Child‑table consistency** – If we later add write‑only child tables, we must keep them in sync with the JSON payload. Mitigation: generate child rows *once* from the payload during the same transaction. |
| **Idempotency‑key collisions** – Clients must generate truly unique keys. Mitigation: treat collisions as legitimate retries (return existing investigation). |
| **Migration complexity for future phases** – Adding more relational data (e.g., RBAC) will require additional migrations. Mitigation: keep the initial migration minimal and well‑documented. |
| **Test environment reliability** – Spinning up a PostgreSQL container adds flakiness. Mitigation: use a deterministic Docker image (`postgres:15-alpine`) and ensure proper teardown in fixtures.

---

*Prepared by Claude Code – this audit incorporates the mandatory constraints and is ready for stakeholder approval before any implementation begins.*
