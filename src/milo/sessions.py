"""Persistent, bounded conversation sessions."""

from __future__ import annotations

import builtins
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .storage import SQLiteStore


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Session:
    id: str
    project: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)


class SessionStore:
    def __init__(self, database: str | Path, *, max_messages: int = 200) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self.max_messages = max_messages
        self._store = SQLiteStore(database)
        self._db = self._store.connection
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_project_updated
                ON sessions(project, updated_at DESC);
            CREATE INDEX IF NOT EXISTS messages_session_id
                ON messages(session_id, id);
            """
        )
        self._db.commit()

    def create(self, project: str, metadata: dict[str, Any] | None = None) -> Session:
        now = datetime.now(UTC)
        identifier = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (identifier, project, json.dumps(metadata or {}), now.isoformat(), now.isoformat()),
        )
        self._db.commit()
        result = self.get(identifier)
        assert result is not None
        return result

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        if not self._db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise KeyError(session_id)
        now = datetime.now(UTC)
        with self._db:
            self._db.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, json.dumps(metadata or {}), now.isoformat()),
            )
            self._db.execute(
                "DELETE FROM messages WHERE session_id = ? AND id NOT IN "
                "(SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, self.max_messages),
            )
            self._db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now.isoformat(), session_id)
            )
        return Message(role, content, now, metadata or {})

    def get(self, session_id: str) -> Session | None:
        row = self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        messages = self._db.execute(
            "SELECT role, content, metadata, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return Session(
            id=row["id"],
            project=row["project"],
            metadata=json.loads(row["metadata"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            messages=[
                Message(
                    item["role"],
                    item["content"],
                    datetime.fromisoformat(item["created_at"]),
                    json.loads(item["metadata"]),
                )
                for item in messages
            ],
        )

    def list(self, project: str | None = None, *, limit: int = 100) -> list[Session]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        sql = "SELECT id FROM sessions"
        parameters: tuple[object, ...]
        if project is None:
            sql += " ORDER BY updated_at DESC LIMIT ?"
            parameters = (limit,)
        else:
            sql += " WHERE project = ? ORDER BY updated_at DESC LIMIT ?"
            parameters = (project, limit)
        return [item for row in self._db.execute(sql, parameters) if (item := self.get(row["id"]))]

    def search(
        self, query: str, *, project: str | None = None, limit: int = 20
    ) -> builtins.list[Session]:
        if not query.strip() or limit <= 0:
            return []
        escaped = query.replace("%", r"\%").replace("_", r"\_")
        sql = (
            "SELECT DISTINCT s.id FROM sessions s "
            "JOIN messages m ON m.session_id = s.id "
            "WHERE m.content LIKE ? ESCAPE '\\'"
        )
        parameters: list[object] = [f"%{escaped}%"]
        if project is not None:
            sql += " AND s.project = ?"
            parameters.append(project)
        sql += " ORDER BY s.updated_at DESC LIMIT ?"
        parameters.append(limit)
        return [
            item
            for row in self._db.execute(sql, parameters)
            if (item := self.get(row["id"])) is not None
        ]

    def delete(self, session_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def prune(self, *, older_than: timedelta, project: str | None = None) -> int:
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        if project is None:
            cursor = self._db.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        else:
            cursor = self._db.execute(
                "DELETE FROM sessions WHERE project = ? AND updated_at < ?", (project, cutoff)
            )
        self._db.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
