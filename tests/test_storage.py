from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path

from milo.memory import MemoryStore
from milo.sessions import SessionStore
from milo.storage import SQLiteStore, resolve_milo_home


def test_sqlite_store_is_created_with_private_permissions(tmp_path: Path) -> None:
    home = tmp_path / "state"

    store = SQLiteStore(home / "milo.db")
    store.execute("CREATE TABLE example (value TEXT)")
    store.close()

    assert resolve_milo_home(home) == home.resolve()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "milo.db").stat().st_mode) == 0o600


def test_sessions_are_persistent_and_messages_are_bounded(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    sessions = SessionStore(database, max_messages=2)
    session = sessions.create(project="alpha", metadata={"model": "test"})
    sessions.add_message(session.id, "user", "one")
    sessions.add_message(session.id, "assistant", "two")
    sessions.add_message(session.id, "user", "three")
    sessions.close()

    reopened = SessionStore(database, max_messages=2)
    loaded = reopened.get(session.id)

    assert loaded is not None
    assert loaded.project == "alpha"
    assert loaded.metadata == {"model": "test"}
    assert [(item.role, item.content) for item in loaded.messages] == [
        ("assistant", "two"),
        ("user", "three"),
    ]


def test_sessions_can_be_listed_deleted_and_pruned(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "state.db")
    first = sessions.create(project="alpha")
    second = sessions.create(project="beta")

    assert [item.id for item in sessions.list(project="alpha")] == [first.id]
    assert sessions.delete(first.id)
    assert sessions.get(first.id) is None
    assert sessions.prune(older_than=timedelta(seconds=-1)) == 1
    assert sessions.get(second.id) is None


def test_memory_is_project_aware_searchable_editable_and_redacted(tmp_path: Path) -> None:
    memory = MemoryStore(
        tmp_path / "memory.db",
        redactor=lambda text: text.replace("token-123", "[secret]"),
    )
    alpha_id = memory.add(
        "alpha",
        "Deploy orchids using token-123",
        metadata={"kind": "decision"},
    )
    memory.add("beta", "Deploy orchids differently")

    hits = memory.search("alpha", "orchids")
    assert len(hits) == 1
    assert hits[0].content == "Deploy orchids using [secret]"
    assert hits[0].metadata == {"kind": "decision"}

    assert memory.edit(alpha_id, content="Grow orchids indoors", metadata={"kind": "fact"})
    assert [item.content for item in memory.search("alpha", "indoors")] == ["Grow orchids indoors"]
    assert memory.remove(alpha_id)
    assert memory.search("alpha", "indoors") == []


def test_memory_is_bounded_per_project_and_can_be_pruned(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path / "memory.db", max_entries_per_project=2)
    memory.add("alpha", "first note")
    memory.add("alpha", "second note")
    memory.add("alpha", "third note")

    assert [item.content for item in memory.list("alpha")] == ["third note", "second note"]
    assert memory.prune(project="alpha", keep=1) == 1
    assert [item.content for item in memory.list("alpha")] == ["third note"]
