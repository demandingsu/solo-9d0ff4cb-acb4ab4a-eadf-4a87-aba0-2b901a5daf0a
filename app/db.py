"""Database engine, session helpers and concurrency primitives."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .errors import StaleVersionError
from .models import Base

_connect_args: dict = {}
if not config.IS_POSTGRES:
    # Needed for SERIALIZABLE / FK enforcement on SQLite.
    _connect_args = {"timeout": 30, "check_same_thread": False}

engine: Engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if not config.IS_POSTGRES:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin_immediate(conn):
        # Serialize writer transactions up-front, mirroring the PostgreSQL
        # advisory lock. The second writer waits for the first to commit,
        # then its optimistic version check reliably rejects the stale
        # submission instead of failing on a deferred unique index.
        conn.connection.dbapi_connection.execute("BEGIN IMMEDIATE")


def init_db() -> None:
    """Create all tables. Safe to call on every startup (idempotent DDL)."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        msg = str(getattr(exc, "orig", exc)).lower()
        # Only unique-index races (another writer committed first) map to the
        # stale-snapshot contract; CHECK / FK errors keep their normal 4xx.
        if "unique" in msg or "duplicate key" in msg:
            raise StaleVersionError(
                submitted=-1, current=-1,
                message="并发写入冲突：该田块已被其他实例/用户修改，请重新获取快照后再提交",
            ) from exc
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def snapshot_session() -> Iterator[Session]:
    """A transaction whose reads come from one consistent snapshot.

    * PostgreSQL: REPEATABLE READ — every statement in the transaction sees the
      snapshot established by the first read, so the reading pairs and their
      conclusions returned by a review query always come from the same
      snapshot.
    * SQLite (demo/tests): SERIALIZABLE is the only meaningful level and is
      enforced via a single deferred transaction.
    """
    session = SessionLocal()
    if config.IS_POSTGRES:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    # SQLite: the session's own deferred transaction already provides a
    # consistent snapshot (single WAL snapshot) for all reads inside it.
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def transaction_snapshot_id(session: Session) -> int:
    """A token identifying the current committed database state (SQLite demo).

    The primary concurrency token used by the API is
    ``field_versions.version`` (see services/common.py); this helper is kept
    for diagnostics/tests on the SQLite demo path.
    """
    return session.execute(text("PRAGMA data_version")).scalar_one()


@contextmanager
def field_lock(session: Session, field_id: int) -> Iterator[None]:
    """Serialize mutating transactions touching one field.

    PostgreSQL uses a transaction-scoped advisory lock so two concurrent
    uploads/reviews on the same field cannot interleave. SQLite serializes
    writes database-wide through its write lock already.
    """
    if config.IS_POSTGRES:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"field:{field_id}"},
        )
    yield
