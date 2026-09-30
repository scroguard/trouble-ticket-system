"""Database engine and session factory."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,  # transparently replace connections dropped by PG restarts
    pool_size=10,
    max_overflow=10,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts/workers/background tasks:
    commit on success, rollback on error, always close."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one transaction per request.

    Commits when the endpoint returns normally, rolls back if it raises (including
    HTTPException), and always returns the connection to the pool. FastAPI runs this
    exit code before the response is sent, so a failed commit becomes a 500 rather
    than a false success. Endpoints that must persist something *and* return an error
    (e.g. counting a failed login) commit explicitly before raising.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
