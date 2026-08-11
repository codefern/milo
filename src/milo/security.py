"""Security primitives shared by Milo subsystems."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(authorization|api[_-]?key|token|secret|password|passwd|credential)(?:$|[_-])",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b")


class SecurityError(ValueError):
    """Raised when an operation violates a security policy."""


def redact_secrets(value: Any) -> Any:
    """Return a recursively redacted copy of JSON-like data."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _SECRET_KEY.search(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        redacted = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}={REDACTED}", value)
        redacted = _BEARER_SECRET.sub(f"Bearer {REDACTED}", redacted)
        return _KNOWN_TOKEN.sub(REDACTED, redacted)
    return value


class PathPolicy:
    """Authorize canonical paths under a workspace with immutable protected roots."""

    def __init__(
        self,
        workspace: str | PathLike[str],
        *,
        protected_roots: Iterable[str | PathLike[str]] = ("/opt/va-backend",),
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise SecurityError("workspace must be a directory")
        self.protected_roots = tuple(Path(path).resolve(strict=False) for path in protected_roots)

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def authorize(self, path: str | PathLike[str], *, write: bool = False) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        canonical = candidate.resolve(strict=False)
        if write and any(self._within(canonical, root) for root in self.protected_roots):
            raise SecurityError("protected path is immutable")
        if not self._within(canonical, self.workspace):
            raise SecurityError("path is outside the authorized workspace")
        return canonical


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    risk: RiskLevel
    reason: str
    requires_approval: bool = False


class CommandPolicy:
    """Classify explicit argv commands; shell strings are never accepted."""

    _approval_subcommands = {
        ("git", "push"),
        ("git", "reset"),
        ("git", "clean"),
        ("gh", "repo"),
        ("gh", "pr"),
    }
    _blocked_commands = {"sh", "bash", "zsh", "fish", "sudo", "su", "eval"}
    _read_only_git = {"rev-parse", "ls-files"}

    def __init__(self, *, allowed_commands: Iterable[str]) -> None:
        self.allowed_commands = frozenset(allowed_commands)

    def evaluate(self, argv: object, *, approved: bool = False) -> PolicyDecision:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
            return PolicyDecision(
                False,
                RiskLevel.CRITICAL,
                "commands must be an argv sequence; shell execution is disabled",
            )
        if not argv or not all(isinstance(arg, str) and arg for arg in argv):
            return PolicyDecision(False, RiskLevel.HIGH, "argv must contain non-empty strings")
        command = Path(argv[0]).name
        if argv[0] != command:
            return PolicyDecision(False, RiskLevel.HIGH, "executable paths are not allowed")
        if command in self._blocked_commands:
            return PolicyDecision(False, RiskLevel.CRITICAL, "shell execution is disabled")
        if command not in self.allowed_commands:
            return PolicyDecision(False, RiskLevel.HIGH, "command is not allowlisted")
        if command == "git":
            unsafe = any(
                arg == "-c"
                or "alias." in arg.casefold()
                or arg.startswith(("!", "ext::", "--exec-path", "--upload-pack", "--receive-pack"))
                or any(token in arg.casefold() for token in ("pager", "ext-diff", "textconv"))
                for arg in argv[1:]
            )
            if unsafe:
                return PolicyDecision(False, RiskLevel.CRITICAL, "unsafe Git execution option")
            subcommand = argv[1] if len(argv) > 1 else ""
            if subcommand not in self._read_only_git and not approved:
                return PolicyDecision(False, RiskLevel.HIGH, "explicit approval required", True)
        key = (command, argv[1]) if len(argv) > 1 else (command, "")
        if key in self._approval_subcommands and not approved:
            return PolicyDecision(False, RiskLevel.HIGH, "explicit approval required", True)
        risk = RiskLevel.HIGH if key in self._approval_subcommands else RiskLevel.LOW
        return PolicyDecision(True, risk, "allowed")

    def authorize(self, argv: object, *, approved: bool = False) -> tuple[str, ...]:
        decision = self.evaluate(argv, approved=approved)
        if not decision.allowed:
            raise SecurityError(decision.reason)
        normalized: tuple[str, ...] = tuple(argv)  # type: ignore[arg-type]
        if normalized[0] == "git" and len(normalized) > 1 and normalized[1] in self._read_only_git:
            return (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *normalized[1:],
            )
        return normalized
