"""The data layer: one SQLite file, SQLAlchemy models, Alembic migrations.

``<home>/state/state.db`` holds every table lantern persists: the engine and
daemon state, the remote operations API, and the local collaboration records
used by clients such as Lantern.
:mod:`lantern.db.session` builds the connections, :mod:`lantern.db.schema`
applies the migrations, and :mod:`lantern.db.base` carries the metadata the
models hang off.
"""

from __future__ import annotations

from lantern.db.base import Base
from lantern.db.schema import current_revision, ensure_schema, head_revision
from lantern.db.session import BUSY_TIMEOUT_MS, begin_immediate, open_engine, readonly_uri

__all__ = [
    "BUSY_TIMEOUT_MS",
    "Base",
    "begin_immediate",
    "current_revision",
    "ensure_schema",
    "head_revision",
    "open_engine",
    "readonly_uri",
]
