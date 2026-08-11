from __future__ import annotations

import json
from pathlib import Path

import pytest

from milo.agent import Agent
from milo.automation import AutomationStore
from milo.checkpoints import CheckpointStore
from milo.config import Config, ConfigStore
from milo.mcp import MCPConfig, MCPError, provider_add_argv
from milo.orchestrator import DelegationDecision, Orchestrator
from milo.providers import ClaudeProvider, CodexProvider, GeminiProvider


class FakeRunner:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.lines = lines or ['{"type":"result","result":"done","session_id":"provider-1"}\n']

    def run(self, argv: list[str]):
        self.calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    def stream(self, argv: list[str]):
        self.calls.append(argv)
        return iter(self.lines)


def test_provider_invocations_are_provider_specific() -> None:
    runner = FakeRunner()
    codex = CodexProvider(runner=runner)
    claude = ClaudeProvider(runner=runner)
    gemini = GeminiProvider(runner=runner)

    list(codex.stream("fix it", model="gpt-5", session_id="s1"))
    list(claude.stream("fix it", model="sonnet", session_id="s2"))
    list(gemini.stream("fix it", model="gemini-2.5-pro", session_id="latest"))

    assert runner.calls[0] == [
        "codex",
        "--ask-for-approval",
        "untrusted",
        "exec",
        "-c",
        'model_reasoning_effort="low"',
        "--sandbox",
        "workspace-write",
        "resume",
        "s1",
        "--json",
        "--model",
        "gpt-5",
        "fix it",
    ]
    assert runner.calls[1] == [
        "claude",
        "-p",
        "fix it",
        "--effort",
        "low",
        "--permission-mode",
        "default",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        "sonnet",
        "--resume",
        "s2",
    ]
    assert runner.calls[2] == [
        "gemini",
        "--prompt",
        "fix it",
        "--output-format",
        "stream-json",
        "--approval-mode",
        "default",
        "--model",
        "gemini-2.5-pro",
        "--resume",
        "latest",
    ]


def test_config_is_private_validated_and_round_trips(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    config = Config(provider="codex", model="gpt-5", max_agents=3)
    store.save(config)
    assert store.load() == config
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        store.save(Config(provider="opencode"))


def test_delegation_is_dynamic_and_bounded() -> None:
    assert Orchestrator.decide("rename this variable") == DelegationDecision(
        False, (), "simple task"
    )
    decision = Orchestrator.decide(
        "Research three providers, implement adapters, run integration tests, and perform a security review",
        max_agents=3,
    )
    assert decision.delegate is True
    assert 2 <= len(decision.roles) <= 3
    assert "researcher" in decision.roles
    assert "tester" in decision.roles


def test_checkpoint_create_restore_and_path_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "app.py"
    file.write_text("before")
    checkpoints = CheckpointStore(tmp_path / "state", workspace)
    checkpoint = checkpoints.create("safe point", ["app.py"])
    file.write_text("after")
    checkpoints.restore(checkpoint.id)
    assert file.read_text() == "before"
    with pytest.raises(ValueError):
        checkpoints.create("bad", ["../outside"])


def test_automation_requires_explicit_enable_and_is_editable(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "automation.json")
    job = store.create("daily-check", "0 9 * * *", "run tests")
    assert job.enabled is False
    assert store.set_enabled(job.id, True).enabled is True
    assert store.remove(job.id) is True


def test_mcp_config_filters_environment_and_rejects_unsafe_transport() -> None:
    config = MCPConfig.from_dict({"name": "time", "command": "uvx", "args": ["mcp-server-time"]})
    env = config.subprocess_environment(
        {"PATH": "/bin", "HOME": "/tmp", "OPENAI_API_KEY": "secret"}
    )
    assert env == {"PATH": "/bin", "HOME": "/tmp"}
    with pytest.raises(MCPError):
        MCPConfig.from_dict({"name": "bad", "command": "bash", "args": ["-c", "x"]})
    with pytest.raises(MCPError):
        MCPConfig.from_dict({"name": "local", "url": "https://127.0.0.1/mcp"})
    remote = MCPConfig.from_dict({"name": "docs", "url": "https://example.com/mcp"})
    assert provider_add_argv("codex", remote) == [
        "codex",
        "mcp",
        "add",
        "docs",
        "--url",
        "https://example.com/mcp",
    ]


def test_memory_defaults_to_secret_redaction(tmp_path: Path) -> None:
    from milo.memory import MemoryStore

    with MemoryStore(tmp_path / "state.db") as store:
        memory_id = store.add("project", "api_key=super-secret-value")
        assert store.get(memory_id).content == "api_key=[REDACTED]"


def test_catalog_manifest_is_machine_readable_and_recommended_is_curated() -> None:
    catalog = json.loads((Path(__file__).parents[1] / "catalog" / "catalog.json").read_text())
    assert len(catalog["skills"]) >= 15
    recommended = [item for item in catalog["skills"] if item["recommended"]]
    assert 12 <= len(recommended) <= 18
    assert all(
        {"name", "description", "version", "source", "requirements", "compatibility", "install"}
        <= item.keys()
        for item in catalog["skills"]
    )


def test_agent_streams_lead_text_without_delegating(tmp_path: Path) -> None:
    class Provider:
        def detect(self) -> bool:
            return True

        def stream(self, *_args, **_kwargs):
            yield {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "live"},
            }

    streamed: list[str] = []
    result = Agent(
        Config(),
        tmp_path / "state.db",
        project="project",
        provider_factory=lambda _name: Provider(),
        on_text=streamed.append,
    ).run("simple request")
    assert streamed == ["live"]
    assert result.text == "live"
