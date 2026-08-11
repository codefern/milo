"""Relevant workspace context selection with a hard token budget."""

from __future__ import annotations

import math
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

_BINARY_SUFFIXES = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".class",
    ".db",
    ".dll",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".sqlite",
    ".tar",
    ".webp",
    ".woff",
    ".woff2",
    ".xlsx",
    ".zip",
}


def estimate_tokens(text: str) -> int:
    """Conservatively estimate tokens without a provider tokenizer."""
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


@dataclass(frozen=True)
class ContextItem:
    path: Path
    content: str
    tokens: int
    truncated: bool = False
    score: int = 0


@dataclass(frozen=True)
class IgnoredItem:
    path: Path
    reason: str


@dataclass(frozen=True)
class ContextResult:
    items: list[ContextItem] = field(default_factory=list)
    ignored: list[IgnoredItem] = field(default_factory=list)
    token_budget: int = 0
    used_tokens: int = 0

    @property
    def remaining_tokens(self) -> int:
        return self.token_budget - self.used_tokens


class ContextSelector:
    def __init__(self, project_root: str | os.PathLike[str]) -> None:
        self.root = Path(project_root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError("project_root must be a directory")

    def select(
        self,
        *,
        keywords: Iterable[str] = (),
        paths: Iterable[str | os.PathLike[str]] = (),
        token_budget: int,
    ) -> ContextResult:
        if token_budget < 0:
            raise ValueError("token_budget cannot be negative")
        terms = tuple(dict.fromkeys(term.casefold() for term in keywords if term.strip()))
        requested: list[Path] = []
        for raw_path in paths:
            candidate = (self.root / raw_path).resolve()
            if (
                self._is_inside(candidate)
                and candidate.is_file()
                and not candidate.is_symlink()
                and not self._is_sensitive(candidate)
            ):
                requested.append(candidate)

        candidates: dict[Path, tuple[int, str]] = {}
        ignored: list[IgnoredItem] = []
        for path in self._walk_files():
            relative = path.relative_to(self.root)
            if self._is_binary(path):
                ignored.append(IgnoredItem(relative, "binary"))
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                ignored.append(IgnoredItem(relative, "unreadable-or-binary"))
                continue
            requested_score = 10_000 if path in requested else 0
            keyword_score = sum(text.casefold().count(term) for term in terms)
            if requested_score or keyword_score or (not terms and path in requested):
                candidates[path] = (requested_score + keyword_score, text)

        # Explicit paths that are not reached by the walk (for example hidden files) remain safe.
        for path in requested:
            if path in candidates or self._is_binary(path):
                continue
            try:
                candidates[path] = (10_000, path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                ignored.append(IgnoredItem(path.relative_to(self.root), "unreadable-or-binary"))

        ordered = sorted(
            candidates.items(),
            key=lambda pair: (-pair[1][0], pair[0].relative_to(self.root).as_posix()),
        )
        items: list[ContextItem] = []
        remaining = token_budget
        for path, (score, text) in ordered:
            if remaining <= 0:
                break
            disclosed = self._disclose(text, terms, remaining)
            if not disclosed:
                continue
            tokens = estimate_tokens(disclosed)
            items.append(
                ContextItem(
                    path.relative_to(self.root),
                    disclosed,
                    tokens,
                    truncated=disclosed != text,
                    score=score,
                )
            )
            remaining -= tokens
        return ContextResult(items, ignored, token_budget, token_budget - remaining)

    def _walk_files(self) -> Iterable[Path]:
        excluded = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "dist",
            "build",
            ".milo",
        }
        for current, directories, filenames in os.walk(self.root, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if name not in excluded and not (Path(current) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(current) / filename
                if not path.is_symlink() and not self._is_sensitive(path):
                    yield path

    @staticmethod
    def _is_sensitive(path: Path) -> bool:
        name = path.name.casefold()
        return (
            name == ".env"
            or name.startswith(".env.")
            or name in {".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json"}
            or name in {"id_rsa", "id_ed25519"}
            or path.suffix.casefold() in {".key", ".pem", ".p12", ".pfx"}
        )

    def _is_inside(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_binary(path: Path) -> bool:
        if path.suffix.casefold() in _BINARY_SUFFIXES:
            return True
        try:
            sample = path.read_bytes()[:8192]
        except OSError:
            return True
        return b"\0" in sample

    @staticmethod
    def _disclose(text: str, terms: tuple[str, ...], budget: int) -> str:
        max_bytes = budget * 4
        if max_bytes <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        # Progressive disclosure starts near the first relevant line, not blindly at byte zero.
        lowered = text.casefold()
        offsets = [lowered.find(term) for term in terms if term in lowered]
        center = min(offsets) if offsets else 0
        start = max(0, center - max_bytes // 3)
        fragment = encoded[start : start + max_bytes]
        while fragment:
            try:
                return fragment.decode("utf-8")
            except UnicodeDecodeError:
                fragment = fragment[:-1]
        return ""
