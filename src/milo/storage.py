"""Shared SQLite persistence and safe state-directory handling."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def resolve_milo_home(path: str | os.PathLike[str] | None = None) -> Path:
    """Return and securely create Milo's state directory."""
    if path is None:
        configured = os.environ.get("MILO_HOME")
        path = configured if configured else Path.home() / ".milo"
    home = Path(path).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    return home


class SQLiteStore:
    """Small connection wrapper that applies safe defaults."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> sqlite3.Cursor:
        cursor = self.connection.execute(sql, tuple(parameters))
        self.connection.commit()
        return cursor

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
