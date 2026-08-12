from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool
    model_selection: bool
    session_resume: bool
    auth_status: bool


class InvocationError(RuntimeError):
    """A provider process failed."""


class StreamParseError(InvocationError):
    """A provider emitted invalid JSON/JSONL."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, argv: list[str]) -> CommandResult: ...

    def stream(self, argv: list[str]) -> Iterable[str]: ...


class SubprocessRunner:
    def run(self, argv: list[str]) -> CommandResult:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def stream(self, argv: list[str]) -> Iterable[str]:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        yield from process.stdout
        stderr = process.stderr.read() if process.stderr is not None else ""
        if process.wait() != 0:
            raise InvocationError(stderr.strip() or "provider command failed")


_FULL_CAPABILITIES = ProviderCapabilities(True, True, True, True)


class UnsupportedCapabilityError(RuntimeError):
    """Raised when a provider does not expose a requested CLI capability."""


class _Provider(ABC):
    command = ""
    capabilities = _FULL_CAPABILITIES
    auth_status_argv: tuple[str, ...] | None = None
    login_argv: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Runner | None = None,
    ) -> None:
        self._which = which
        self._runner = runner or SubprocessRunner()

    def detect(self) -> bool:
        return self._which(self.command) is not None

    def is_authenticated(self) -> bool:
        if self.auth_status_argv is None:
            raise UnsupportedCapabilityError("authentication status is unsupported")
        return self._runner.run(list(self.auth_status_argv)).returncode == 0

    def login(self) -> None:
        if isinstance(self._runner, SubprocessRunner):
            returncode = subprocess.run(list(self.login_argv), check=False).returncode
            if returncode != 0:
                raise InvocationError("authentication failed; rerun the provider login command")
            return
        result = self._runner.run(list(self.login_argv))
        if result.returncode != 0:
            raise InvocationError(result.stderr.strip() or "authentication failed")

    @abstractmethod
    def invocation(
        self, prompt: str, *, model: str | None = None, session_id: str | None = None
    ) -> list[str]: ...

    def stream(
        self, prompt: str, *, model: str | None = None, session_id: str | None = None
    ) -> Iterator[dict[str, object]]:
        for line in self._runner.stream(
            self.invocation(prompt, model=model, session_id=session_id)
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StreamParseError("provider emitted malformed streaming output") from exc
            if not isinstance(event, dict):
                raise StreamParseError("provider streaming event must be an object")
            if event.get("is_error") is True or event.get("type") == "error":
                detail = event.get("result") or event.get("message") or event.get("error")
                raise InvocationError(str(detail or "provider reported an error"))
            yield event


class CodexProvider(_Provider):
    command = "codex"
    auth_status_argv = ("codex", "login", "status")
    login_argv = ("codex", "login")

    def invocation(
        self, prompt: str, *, model: str | None = None, session_id: str | None = None
    ) -> list[str]:
        argv = [
            "codex",
            "--ask-for-approval",
            "untrusted",
            "exec",
            "-c",
            'model_reasoning_effort="low"',
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
        ]
        if session_id:
            argv.extend(["resume", session_id])
        argv.append("--json")
        if model:
            argv.extend(["--model", model])
        argv.append(prompt)
        return argv


class ClaudeProvider(_Provider):
    command = "claude"
    auth_status_argv = ("claude", "auth", "status")
    login_argv = ("claude", "auth", "login")

    def invocation(
        self, prompt: str, *, model: str | None = None, session_id: str | None = None
    ) -> list[str]:
        argv = [
            "claude",
            "-p",
            prompt,
            "--effort",
            "low",
            "--permission-mode",
            "default",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if model:
            argv.extend(["--model", model])
        if session_id:
            argv.extend(["--resume", session_id])
        return argv


class GeminiProvider(_Provider):
    command = "gemini"
    capabilities = _FULL_CAPABILITIES
    auth_status_argv = None
    login_argv = ("gemini",)

    def is_authenticated(self) -> bool:
        if any(
            os.environ.get(name)
            for name in (
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "GOOGLE_GENAI_USE_VERTEXAI",
                "GOOGLE_GENAI_USE_GCA",
            )
        ):
            return True
        home = Path(os.environ.get("GEMINI_CLI_HOME", Path.home() / ".gemini"))
        if (home / "oauth_creds.json").is_file():
            return True
        settings = home / "settings.json"
        if not settings.is_file():
            return False
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            selected = data.get("security", {}).get("auth", {}).get("selectedType")
            return isinstance(selected, str) and bool(selected)
        except (OSError, json.JSONDecodeError, AttributeError):
            return False

    def invocation(
        self, prompt: str, *, model: str | None = None, session_id: str | None = None
    ) -> list[str]:
        argv = [
            "gemini",
            "--prompt",
            prompt,
            "--output-format",
            "stream-json",
            "--approval-mode",
            "default",
        ]
        if model:
            argv.extend(["--model", model])
        if session_id:
            argv.extend(["--resume", session_id])
        return argv


PROVIDER_TYPES: dict[str, Callable[[], _Provider]] = {
    "codex": CodexProvider,
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
}


def get_provider(name: str) -> _Provider:
    try:
        return PROVIDER_TYPES[name]()
    except KeyError as exc:
        raise ValueError(f"unknown provider: {name}") from exc
