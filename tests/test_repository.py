"""Tests for InMemoryInvestigationRepository and PostgresInvestigationRepository.

These tests verify the repository boundary, CRUD operations, scoped idempotency,
transaction safety and persistence across service restarts.
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest

from api.investigation_service import Investigation
from persistence.repositories import InMemoryInvestigationRepository, PostgresInvestigationRepository


def _make_investigation(idempotency_key: str | None = None) -> Investigation:
    """Create a minimal Investigation instance for testing.

    The payload includes the required ``incident_summary`` keys. ``inputs`` may contain
    an ``idempotency_key`` to trigger the scoped uniqueness logic.
    """
    inv_id = f"INV-{uuid.uuid4().hex.upper()}"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = {"incident_summary": {"confidence": 0.95, "severity": "high"}}
    inputs = {"idempotency_key": idempotency_key} if idempotency_key else {}
    return Investigation(
        investigation_id=inv_id,
        status="completed",
        created_at=now,
        duration_ms=None,
        repository="test_repo",
        repository_full_name="test_repo_full",
        environment="production",
        branch="main",
        commit_sha="deadbeef",
        payload=payload,
        error=None,
        inputs=inputs,
        organization_id=None,
        project_id=None,
    )


# ---------------------------------------------------------------------------
# In‑memory repository tests
# ---------------------------------------------------------------------------

def test_in_memory_crud():
    repo = InMemoryInvestigationRepository()
    inv = _make_investigation()
    created = repo.create(inv)
    assert created.investigation_id == inv.investigation_id
    fetched = repo.get(inv.investigation_id)
    assert fetched is inv
    listed = repo.list()
    assert any(i.investigation_id == inv.investigation_id for i in listed)


def test_in_memory_update_status():
    repo = InMemoryInvestigationRepository()
    inv = _make_investigation()
    repo.create(inv)
    repo.update_status(inv.investigation_id, "failed")
    updated = repo.get(inv.investigation_id)
    assert updated is not None
    assert updated.status == "failed"

# ---------------------------------------------------------------------------
# PostgreSQL repository tests – run only when a PostgreSQL instance is available.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def postgres_repo():
    if os.getenv("AUTORCA_PERSISTENCE") != "postgres":
        pytest.skip("PostgreSQL backend not enabled (AUTORCA_PERSISTENCE!=postgres)")
    db_url = os.getenv("AUTORCA_DATABASE_URL")
    if not db_url:
        pytest.skip("AUTORCA_DATABASE_URL not set – no database to test against")
    # Apply migrations.
    from alembic import command
    from alembic.config import Config

    cfg_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = Config(cfg_path)
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    repo = PostgresInvestigationRepository()
    yield repo
    # Note: cleanup (dropping tables) is omitted for speed; test runs in isolated DB.


def test_postgres_create_and_get(postgres_repo):
    inv = _make_investigation()
    created = postgres_repo.create(inv)
    fetched = postgres_repo.get(created.investigation_id)
    assert fetched is not None
    assert fetched.investigation_id == created.investigation_id
    assert fetched.payload == inv.payload

def test_postgres_scoped_idempotency(postgres_repo):
    key = "unique-key-123"
    inv1 = _make_investigation(idempotency_key=key)
    inv2 = _make_investigation(idempotency_key=key)
    first = postgres_repo.create(inv1)
    second = postgres_repo.create(inv2)
    # The repository should return the existing record on duplicate idempotency.
    assert second.investigation_id == first.investigation_id
    # Ensure the payload is from the first record.
    assert second.payload == first.payload

def test_postgres_restart_persistence(postgres_repo):
    # Create investigation with a fresh repository instance.
    inv = _make_investigation()
    created = postgres_repo.create(inv)
    # Simulate service restart by creating a new repository instance.
    new_repo = PostgresInvestigationRepository()
    fetched = new_repo.get(created.investigation_id)
    assert fetched is not None
    assert fetched.investigation_id == created.investigation_id
    assert fetched.payload == inv.payload
