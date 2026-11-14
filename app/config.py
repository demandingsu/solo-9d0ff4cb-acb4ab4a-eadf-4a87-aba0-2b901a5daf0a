"""Application configuration.

Database URL is taken from the ``DATABASE_URL`` environment variable.  The
production compose stack points it at PostgreSQL; the test-suite and the
self-contained demo can use SQLite so the project stays runnable without a
running database daemon.
"""
from __future__ import annotations

import os


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    # Default keeps the app startable out of the box (demo / CI).
    return "sqlite+pysqlite:///" + os.path.abspath("moisture.db")


def is_postgres(url: str | None = None) -> bool:
    return (url or database_url()).startswith(("postgresql", "postgres"))


DATABASE_URL = database_url()
IS_POSTGRES = is_postgres(DATABASE_URL)

# Humidity is stored as a percentage with one decimal place; the API validates
# this domain range up-front.
HUMIDITY_MIN = 0.0
HUMIDITY_MAX = 100.0
