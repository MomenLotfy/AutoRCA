"""SQLAlchemy ORM models for persistence.

Only the core tables required for Phase 3.1 are defined. Child tables for
observations/evidence/hypotheses are omitted for now because the UI reads the
full ``payload`` JSON column directly.
"""

from __future__ import annotations

from sqlalchemy import Column, String, DateTime, Integer, Float, UniqueConstraint, ForeignKey

from sqlalchemy.dialects.postgresql import JSONB

from persistence.database import Base

class OrganizationModel(Base):
    __tablename__ = "organizations"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)

class ProjectModel(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    organization_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)  # foreign key added via migration

    name = Column(String, nullable=False)

class InvestigationModel(Base):
    __tablename__ = "investigations"

    investigation_id = Column(String, primary_key=True)
    organization_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)

    repository = Column(String, nullable=False)
    repository_full_name = Column(String, nullable=False)
    environment = Column(String, nullable=False)
    branch = Column(String, nullable=False)
    commit_sha = Column(String, nullable=True)
    status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=True)
    severity = Column(String, nullable=True)
    payload = Column(JSONB, nullable=False)
    idempotency_key = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint('project_id', 'idempotency_key', name='uq_project_idempotency_key'),
    )
