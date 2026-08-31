"""Repository abstractions for AutoRCA persistence.

This package defines :class:`InvestigationRepository` – the protocol used by the
service layer – and provides two concrete implementations:

* ``InMemoryInvestigationRepository`` – used by default in the test suite.
* ``PostgresInvestigationRepository`` – stores investigations in PostgreSQL via
  SQLAlchemy models defined in :pymod:`persistence.models`.

Both implementations work with the :class:`api.investigation_service.Investigation`
​dataclass.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional, Protocol

from sqlalchemy.exc import IntegrityError, OperationalError
from persistence.exceptions import PersistenceUnavailableError

from typing import TYPE_CHECKING

# from api.investigation_service import Investigation  # Runtime import removed to avoid circular dependency
from persistence.database import get_session
from persistence.models import OrganizationModel, ProjectModel, InvestigationModel

# ---------------------------------------------------------------------------
# Protocol – the service depends only on this interface.
# ---------------------------------------------------------------------------

class InvestigationRepository(Protocol):
    def create(self, investigation: Investigation) -> Investigation: ...
    def get(self, investigation_id: str) -> Optional[Investigation]: ...
    def list(self) -> List[Investigation]: ...
    def update_status(self, investigation_id: str, new_status: str) -> None: ...

# ---------------------------------------------------------------------------
# In‑memory implementation – used for the default test configuration.
# ---------------------------------------------------------------------------

class InMemoryInvestigationRepository:
    def __init__(self) -> None:
        self._store: dict[str, Investigation] = {}

    def create(self, investigation: Investigation) -> Investigation:
        # Scoped idempotency: if an investigation with the same idempotency_key (and optional project_id)
        # already exists, return the existing record instead of creating a duplicate.
        id_key = investigation.inputs.get("idempotency_key") if investigation.inputs else None
        if id_key is not None:
            for existing in self._store.values():
                if existing.inputs.get("idempotency_key") == id_key:
                    # Optionally enforce same project scope – the in‑memory repo does not store
                    # project_id, so we treat the key as globally unique for simplicity.
                    return existing
        self._store[investigation.investigation_id] = investigation
        return investigation


    def get(self, investigation_id: str) -> Optional[Investigation]:
        return self._store.get(investigation_id)

    def list(self) -> List[Investigation]:
        items = list(self._store.values())
        items.sort(key=lambda inv: inv.created_at, reverse=True)
        return items

    def update_status(self, investigation_id: str, new_status: str) -> None:
        inv = self._store.get(investigation_id)
        if inv:
            from api.investigation_service import Investigation
            updated = Investigation(
                investigation_id=inv.investigation_id,
                status=new_status,
                created_at=inv.created_at,
                duration_ms=inv.duration_ms,
                repository=inv.repository,
                repository_full_name=inv.repository_full_name,
                environment=inv.environment,
                branch=inv.branch,
                commit_sha=inv.commit_sha,
                payload=inv.payload,
                error=inv.error,
                inputs=inv.inputs,
                organization_id=inv.organization_id,
                project_id=inv.project_id,
            )
            self._store[investigation_id] = updated

# ---------------------------------------------------------------------------
# PostgreSQL implementation – respects the unique (project_id, idempotency_key)
# constraint and runs all writes inside a transaction.
# ---------------------------------------------------------------------------

class PostgresInvestigationRepository:
    def __init__(self) -> None:
        # Tables must be created via Alembic; we do not call ``Base.metadata.create_all``.
        # No in‑memory cache; repository instances are stateless regarding persisted investigations.
        pass

    def _ensure_default_org_project(self, db) -> tuple[str, str]:
        """Create a default organization and project if they do not exist.

        Deterministic IDs keep tests stable.
        """
        org_id = "org-default"
        proj_id = "proj-default"
        org = db.get(OrganizationModel, org_id)
        if not org:
            org = OrganizationModel(id=org_id, name="AutoRCA Organization")
            db.add(org)
        proj = db.get(ProjectModel, proj_id)
        if not proj:
            proj = ProjectModel(id=proj_id, organization_id=org_id, name="Default Project")
            db.add(proj)
        return org_id, proj_id

    def create(self, investigation: Investigation) -> Investigation:
        try:
            with get_session() as db:
                org_id, proj_id = self._ensure_default_org_project(db)
                model = InvestigationModel(
                    investigation_id=investigation.investigation_id,
                    organization_id=org_id,
                    project_id=proj_id,
                    repository=investigation.repository,
                    repository_full_name=investigation.repository_full_name,
                    environment=investigation.environment,
                    branch=investigation.branch,
                    commit_sha=investigation.commit_sha,
                    status=investigation.status,
                    created_at=dt.datetime.fromisoformat(investigation.created_at),
                    duration_ms=investigation.duration_ms,
                    confidence=investigation.payload.get("incident_summary", {}).get("confidence"),
                    severity=investigation.payload.get("incident_summary", {}).get("severity"),
                    payload=investigation.payload,
                    idempotency_key=investigation.inputs.get("idempotency_key"),
                )
                db.add(model)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    # Return existing investigation if duplicate idempotency key
                    existing = (
                        db.query(InvestigationModel)
                        .filter(
                            InvestigationModel.project_id == proj_id,
                            InvestigationModel.idempotency_key == model.idempotency_key,
                        )
                        .first()
                    )
                    if existing:
                        return self._orm_to_domain(existing)
                    raise
                db.refresh(model)
                return self._orm_to_domain(model)
        except OperationalError as exc:
            raise PersistenceUnavailableError("Database unavailable") from exc

    def get(self, investigation_id: str) -> Optional[Investigation]:
        try:
            with get_session() as db:
                model = db.get(InvestigationModel, investigation_id)
                if model:
                    return self._orm_to_domain(model)
                return None
        except OperationalError as exc:
            raise PersistenceUnavailableError("Database unavailable") from exc

    def list(self) -> List[Investigation]:
        try:
            with get_session() as db:
                rows = (
                    db.query(InvestigationModel)
                    .order_by(InvestigationModel.created_at.desc())
                    .all()
                )
                return [self._orm_to_domain(r) for r in rows]
        except OperationalError as exc:
            raise PersistenceUnavailableError("Database unavailable") from exc

    def update_status(self, investigation_id: str, new_status: str) -> None:
        try:
            with get_session() as db:
                model = db.get(InvestigationModel, investigation_id)
                if model:
                    model.status = new_status
                    db.commit()
        except OperationalError as exc:
            raise PersistenceUnavailableError("Database unavailable") from exc

    # ---------------------------------------------------------------------
    # Helper conversion
    # ---------------------------------------------------------------------
    def _orm_to_domain(self, model: InvestigationModel) -> Investigation:
        # Import here to avoid circular imports at module load time.
        from api.investigation_service import Investigation
        return Investigation(
            investigation_id=model.investigation_id,
            status=model.status,
            created_at=model.created_at.isoformat(),
            duration_ms=model.duration_ms,
            repository=model.repository,
            repository_full_name=model.repository_full_name,
            environment=model.environment,
            branch=model.branch,
            commit_sha=model.commit_sha,
            payload=model.payload,
            error=None,
            inputs={},
            organization_id=model.organization_id,
            project_id=model.project_id,
        )
