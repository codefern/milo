"""Project-scoped persistent memory backed by SQLite FTS5."""

from __future__ import annotations

import builtins
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import redact_secrets
from .storage import SQLiteStore


@dataclass(frozen=True)
class Memory:
    id: int
    project: str
    content: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryStore:
    def __init__(
        self,
        database: str | Path,
        *,
        max_entries_per_project: int = 1000,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        if max_entries_per_project < 1:
            raise ValueError("max_entries_per_project must be positive")
        self.max_entries_per_project = max_entries_per_project

        def safe_default(value: str) -> str:
            redacted = redact_secrets(value)
            return redacted if isinstance(redacted, str) else value

        self.redactor = redactor or safe_default
        self._store = SQLiteStore(database)
        self._db = self._store.connection
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                content TEXT NOT NULL, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, content='memories', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE INDEX IF NOT EXISTS memories_project_updated
                ON memories(project, updated_at DESC, id DESC);
            """
        )
        self._db.commit()

    def add(self, project: str, content: str, metadata: dict[str, Any] | None = None) -> int:
        now = datetime.now(UTC).isoformat()
        safe_content = self.redactor(content)
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO memories(project, content, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project, safe_content, json.dumps(metadata or {}), now, now),
            )
            self._trim(project, self.max_entries_per_project)
        if cursor.lastrowid is None:
            raise RuntimeError("memory insert did not return an id")
        return int(cursor.lastrowid)

    def get(self, memory_id: int) -> Memory | None:
        row = self._db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._from_row(row) if row else None

    def list(self, project: str, *, limit: int = 100) -> builtins.list[Memory]:
        rows = self._db.execute(
            "SELECT * FROM memories WHERE project = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (project, limit),
        )
        return [self._from_row(row) for row in rows]

    def search(self, project: str, query: str, *, limit: int = 20) -> builtins.list[Memory]:
        terms = re.findall(r"[\w-]+", query, re.UNICODE)
        if not terms or limit <= 0:
            return []
        expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        rows = self._db.execute(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.project = ? "
            "ORDER BY bm25(memories_fts), m.updated_at DESC LIMIT ?",
            (expression, project, limit),
        )
        return [self._from_row(row) for row in rows]

    def edit(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        current = self.get(memory_id)
        if current is None:
            return False
        next_content = current.content if content is None else self.redactor(content)
        next_metadata = current.metadata if metadata is None else metadata
        cursor = self._db.execute(
            "UPDATE memories SET content = ?, metadata = ?, updated_at = ? WHERE id = ?",
            (next_content, json.dumps(next_metadata), datetime.now(UTC).isoformat(), memory_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def remove(self, memory_id: int) -> bool:
        cursor = self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def prune(
        self,
        *,
        project: str | None = None,
        keep: int | None = None,
        older_than: timedelta | None = None,
    ) -> int:
        before = self._db.execute("SELECT count(*) FROM memories").fetchone()[0]
        with self._db:
            if older_than is not None:
                cutoff = (datetime.now(UTC) - older_than).isoformat()
                if project is None:
                    self._db.execute("DELETE FROM memories WHERE updated_at < ?", (cutoff,))
                else:
                    self._db.execute(
                        "DELETE FROM memories WHERE project = ? AND updated_at < ?",
                        (project, cutoff),
                    )
            if keep is not None:
                if keep < 0:
                    raise ValueError("keep cannot be negative")
                projects = (
                    [project]
                    if project is not None
                    else [
                        row[0] for row in self._db.execute("SELECT DISTINCT project FROM memories")
                    ]
                )
                for item in projects:
                    self._trim(item, keep)
        after = self._db.execute("SELECT count(*) FROM memories").fetchone()[0]
        return int(before - after)

    def _trim(self, project: str, keep: int) -> None:
        self._db.execute(
            "DELETE FROM memories WHERE project = ? AND id NOT IN "
            "(SELECT id FROM memories WHERE project = ? ORDER BY updated_at DESC, id DESC LIMIT ?)",
            (project, project, keep),
        )

    @staticmethod
    def _from_row(row: Any) -> Memory:
        return Memory(
            id=row["id"],
            project=row["project"],
            content=row["content"],
            metadata=json.loads(row["metadata"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
