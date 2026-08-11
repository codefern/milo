from __future__ import annotations

from milo.providers import (
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    ProviderCapabilities,
)


def test_codex_detection_uses_path_lookup() -> None:
    seen: list[str] = []

    def which(command: str) -> str | None:
        seen.append(command)
        return "/usr/bin/codex"

    provider = CodexProvider(which=which)

    assert provider.detect() is True
    assert seen == ["codex"]


def test_provider_capabilities_are_explicit() -> None:
    expected = ProviderCapabilities(
        streaming=True, model_selection=True, session_resume=True, auth_status=True
    )
    assert CodexProvider().capabilities == expected
    assert ClaudeProvider().capabilities == expected
    assert GeminiProvider().capabilities == expected


def test_codex_auth_status_uses_official_command() -> None:
    class Runner:
        def __init__(self) -> None:
            self.argv: list[str] = []

        def run(self, argv: list[str]):
            self.argv = argv
            return type("Result", (), {"returncode": 0, "stdout": "Logged in", "stderr": ""})()

    runner = Runner()
    provider = CodexProvider(runner=runner)

    assert provider.is_authenticated() is True
    assert runner.argv == ["codex", "login", "status"]


def test_login_uses_official_interfaces() -> None:

    class Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, argv: list[str]):
            self.calls.append(argv)
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    runner = Runner()
    CodexProvider(runner=runner).login()
    ClaudeProvider(runner=runner).login()
    GeminiProvider(runner=runner).login()
    assert runner.calls == [["codex", "login"], ["claude", "auth", "login"], ["gemini"]]


def test_stream_parses_jsonl_and_builds_provider_argv() -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, argv: list[str]):
            raise AssertionError("run should not be used")

        def stream(self, argv: list[str]):
            self.calls.append(argv)
            return iter(['{"type":"message","text":"ok"}\n', "\n"])

    runner = Runner()
    providers = [
        CodexProvider(runner=runner),
        ClaudeProvider(runner=runner),
        GeminiProvider(runner=runner),
    ]

    for provider in providers:
        assert list(provider.stream("hello", model="fast", session_id="abc")) == [
            {"type": "message", "text": "ok"}
        ]

    assert runner.calls == [
        [
            "codex",
            "--ask-for-approval",
            "untrusted",
            "exec",
            "-c",
            'model_reasoning_effort="low"',
            "--sandbox",
            "workspace-write",
            "resume",
            "abc",
            "--json",
            "--model",
            "fast",
            "hello",
        ],
        [
            "claude",
            "-p",
            "hello",
            "--effort",
            "low",
            "--permission-mode",
            "default",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            "fast",
            "--resume",
            "abc",
        ],
        [
            "gemini",
            "--prompt",
            "hello",
            "--output-format",
            "stream-json",
            "--approval-mode",
            "default",
            "--model",
            "fast",
            "--resume",
            "abc",
        ],
    ]
