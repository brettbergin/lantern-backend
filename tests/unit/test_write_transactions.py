"""A transaction that will write takes SQLite's write lock before it reads.

The state database is one WAL file with several connections onto it in the
same process (the daemon's store, the engine's, the console's) and more
beside it (a CLI command). Under WAL a deferred ``BEGIN`` pins a read
snapshot at its first SELECT; when any other connection commits before the
transaction's first write, SQLite cannot upgrade the stale snapshot and
fails that write with ``database is locked`` at once — the busy timeout is
never consulted. These tests hold the rule for every store's committing
session, and hold the two things the rule must not cost: a read takes no
write lock, and slow work is not done under one.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from lantern.api import collaboration as collaboration_module
from lantern.api.auth import store as auth_module
from lantern.api.auth.store import ApiAuthStore, AuthError, StandaloneSessions
from lantern.api.collaboration import CollaborationStore
from lantern.daemon.controls.principal import ALL_CAPABILITIES
from lantern.daemon.store import DaemonStore
from lantern.db.api_models import ClientRow
from lantern.db.engine_models import Run
from lantern.engine.store import StateStore

Opener = Callable[[], AbstractContextManager[Session]]


class Stores:
    """Both of the daemon's stores over one file, and a third connection
    that will not wait: what another writer sees, without the busy timeout
    hiding whether it was held off."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.store = StateStore(path)
        self.dstore = DaemonStore(path)
        self.standalone = StandaloneSessions(path, owns_schema=False)
        self.other = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
        self.store.create_run("r1", "x")

    def commit_elsewhere(self) -> str:
        """Try to commit a write from the third connection, now."""
        try:
            self.other.execute("UPDATE runs SET outcome = outcome WHERE run_id = 'r1'")
        except sqlite3.OperationalError as exc:
            return f"held off: {exc}"
        return "committed"

    def close(self) -> None:
        self.other.close()
        self.standalone.close()
        self.dstore.close()
        self.store.close()


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[Stores]:
    built = Stores(tmp_path / "state.db")
    yield built
    built.close()


WRITERS = {
    "daemon store": lambda s: s.dstore.transaction,
    "engine store": lambda s: s.store._write,
    "standalone sessions": lambda s: s.standalone.transaction,
}


@pytest.mark.parametrize("writer", WRITERS)
def test_a_write_transaction_that_reads_first_holds_other_writers_off(
    stores: Stores, writer: str
) -> None:
    """The read and the write are one step against every other connection.

    The other connection tries to commit between the two. Deferred, it
    would succeed and the write after it would fail with ``database is
    locked``; holding the write lock from the read, it is the other writer
    that waits — which, unlike the snapshot upgrade, the busy timeout
    covers.
    """
    opener: Opener = WRITERS[writer](stores)
    with opener() as session:
        assert session.scalar(select(Run.outcome).where(Run.run_id == "r1")) == "x"
        assert stores.commit_elsewhere().startswith("held off")
        session.execute(update(Run).where(Run.run_id == "r1").values(outcome="y"))
    assert stores.store.get_run("r1").outcome == "y"
    assert stores.commit_elsewhere() == "committed"


def test_a_read_takes_no_write_lock(stores: Stores) -> None:
    """A query stays deferred: another connection commits beside it."""
    with stores.dstore.read() as session:
        session.scalar(select(Run.outcome).where(Run.run_id == "r1"))
        assert stores.commit_elsewhere() == "committed"
    with stores.store._read() as session:
        session.scalar(select(Run.outcome).where(Run.run_id == "r1"))
        assert stores.commit_elsewhere() == "committed"


def test_a_write_transaction_takes_the_lock_at_its_first_statement(stores: Stores) -> None:
    """Not on entry: what a block computes before it touches the database
    is not done under the write lock, and a block that never touches it
    takes none."""
    with stores.dstore.transaction() as session:
        assert stores.commit_elsewhere() == "committed"
        session.scalar(select(Run.outcome).where(Run.run_id == "r1"))
        assert stores.commit_elsewhere().startswith("held off")


def test_a_read_only_store_still_reads_through_its_write_session(tmp_path: Path) -> None:
    """The console's handle cannot take a write lock, and a block of its
    that only reads must not ask for one."""
    path = tmp_path / "state.db"
    StateStore(path).close()
    DaemonStore(path).close()
    for readonly in (DaemonStore(path, readonly=True), StateStore(path, readonly=True)):
        try:
            with readonly._write() as session:
                assert session.scalar(select(Run.outcome).where(Run.run_id == "none")) is None
        finally:
            readonly.close()


class TestAuthenticate:
    """Verifying a secret is scrypt — tens of milliseconds on purpose. It
    runs with no transaction open and outside the store's lock, and what
    it writes afterwards is one statement."""

    @pytest.fixture
    def auth(self, stores: Stores) -> ApiAuthStore:
        return ApiAuthStore(stores.dstore)

    def test_another_connection_commits_during_the_check(
        self, stores: Stores, auth: ApiAuthStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field failure: ``POST /v1/auth/local/login`` answered 500 under
        load. The client row was read, the secret checked, and
        ``last_used_at`` written in one deferred transaction; a run's event
        committed on the engine's connection during the check and the write
        failed with ``database is locked``."""
        client, secret = auth.create_client("t", ALL_CAPABILITIES, created_by="test", now=1.0)
        real, seen = auth_module.check_secret, []

        def check(candidate: str, stored: str) -> bool:
            seen.append(stores.commit_elsewhere())
            return real(candidate, stored)

        monkeypatch.setattr(auth_module, "check_secret", check)
        assert auth.authenticate(client.id, secret, 2.0).last_used_at == 2.0
        assert seen == ["committed"]
        assert auth.get_client(client.id).last_used_at == 2.0  # type: ignore[union-attr]

    def test_the_store_is_not_held_during_the_check(
        self, stores: Stores, auth: ApiAuthStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every statement on the daemon's store runs under one lock; a
        sign-in that held it across the check stalled the daemon and every
        other request for as long as the hash took."""
        client, secret = auth.create_client("t", ALL_CAPABILITIES, created_by="test", now=1.0)
        real, seen = auth_module.check_secret, []

        def check(candidate: str, stored: str) -> bool:
            reader = threading.Thread(target=lambda: seen.append(auth.get_client(client.id)))
            reader.start()
            reader.join(timeout=5)
            seen.append("stalled" if reader.is_alive() else "served")
            return real(candidate, stored)

        monkeypatch.setattr(auth_module, "check_secret", check)
        auth.authenticate(client.id, secret, 2.0)
        assert seen[-1] == "served"

    def test_a_client_revoked_during_the_check_is_refused(
        self, auth: ApiAuthStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write is conditional on what the check was made against, so
        moving the check out of the transaction opens no window."""
        client, secret = auth.create_client("t", ALL_CAPABILITIES, created_by="test", now=1.0)
        real = auth_module.check_secret

        def check(candidate: str, stored: str) -> bool:
            auth.revoke_client(client.id, 2.0)
            return real(candidate, stored)

        monkeypatch.setattr(auth_module, "check_secret", check)
        with pytest.raises(AuthError) as refused:
            auth.authenticate(client.id, secret, 3.0)
        assert refused.value.code == "invalid_client"
        assert auth.get_client(client.id).last_used_at is None  # type: ignore[union-attr]

    def test_a_secret_replaced_during_the_check_is_refused(
        self, stores: Stores, auth: ApiAuthStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, secret = auth.create_client("t", ALL_CAPABILITIES, created_by="test", now=1.0)
        real = auth_module.check_secret

        def check(candidate: str, stored: str) -> bool:
            with stores.dstore.transaction() as session:
                session.execute(
                    update(ClientRow)
                    .where(ClientRow.id == client.id)
                    .values(secret_hash=auth_module.hash_secret("another secret"))
                )
            return real(candidate, stored)

        monkeypatch.setattr(auth_module, "check_secret", check)
        with pytest.raises(AuthError):
            auth.authenticate(client.id, secret, 3.0)

    def test_wrong_and_unknown_are_refused_alike_and_write_nothing(
        self, stores: Stores, auth: ApiAuthStore
    ) -> None:
        client, _secret = auth.create_client("t", ALL_CAPABILITIES, created_by="test", now=1.0)
        statements: list[str] = []

        def record(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
            statements.append(sql.split()[0].upper())

        event.listen(stores.dstore._engine, "before_cursor_execute", record)
        try:
            for client_id in (client.id, "api_nobody"):
                with pytest.raises(AuthError) as refused:
                    auth.authenticate(client_id, "not the secret", 2.0)
                assert refused.value.code == "invalid_client"
        finally:
            event.remove(stores.dstore._engine, "before_cursor_execute", record)
        assert "UPDATE" not in statements and "BEGIN IMMEDIATE" not in statements


def test_registering_hashes_the_password_before_it_takes_the_write_lock(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration counts the users, then inserts one with a scrypt hash:
    the hash is made first, so the count and the insert hold the write lock
    for two statements rather than for the hash."""
    collaboration = CollaborationStore(stores.dstore)
    real, seen = collaboration_module.hash_secret, []

    def hash_secret(secret: str, **kwargs: Any) -> str:
        seen.append(stores.commit_elsewhere())
        return real(secret, **kwargs)

    monkeypatch.setattr(collaboration_module, "hash_secret", hash_secret)
    user = collaboration.register_user(
        username="alice",
        email="alice@example.test",
        password="correct horse battery staple",
        full_name=None,
        timezone="UTC",
        now=1.0,
        invite_token=None,
    )
    assert seen == ["committed"]
    assert collaboration.user_by_username("alice") == user
