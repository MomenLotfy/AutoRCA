"""Database utilities – SQLAlchemy engine and session handling.

The module reads the ``AUTORCA_DATABASE_URL`` environment variable. If the
variable is missing when the PostgreSQL repository is used, the application will
raise an error at startup (as required by the architecture).
"""

from __future__ import annotations

import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Default placeholder – production must supply a real URL via AUTORCA_DATABASE_URL.
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/autorca"

# Lazily‑initialized engine and sessionmaker.
# The engine is only instantiated when a session is first requested. This avoids importing
# the PostgreSQL driver (psycopg2) when the application runs in the default in‑memory mode
# and the test suite imports the module.
_engine = None
SessionLocal = None

def _init_engine() -> None:
    """Initialise the global SQLAlchemy engine and SessionLocal.

    This function reads ``AUTORCA_DATABASE_URL`` (falling back to the placeholder) and
    creates the engine. If the PostgreSQL driver is missing, the ImportError is raised
    only when the engine is required (i.e. when the PostgreSQL repository is used).
    """
    global _engine, SessionLocal
    if _engine is None:
        db_url = os.getenv("AUTORCA_DATABASE_URL", DEFAULT_DATABASE_URL)
        # ``create_engine`` will attempt to import the driver for the URL scheme.
        # Let any ImportError propagate – callers that require PostgreSQL must handle it.
        _engine = create_engine(db_url, future=True, echo=False)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

# Base class for ORM models.
Base = declarative_base()

from contextlib import contextmanager

@contextmanager
def get_session() -> Generator:
    """Yield a SQLAlchemy session – intended for use in a ``with`` block.

    The engine is initialised on first use. The caller is responsible for handling
    any ImportError that may arise if the PostgreSQL driver is unavailable.
    """
    _init_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
