from __future__ import annotations

import argparse
import json
import os
import subprocess
from argparse import Namespace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from milo import cli
from milo import update as update_module
from milo.agent import Agent
from milo.automation import AutomationStore
from milo.checkpoints import CheckpointStore
from milo.config import Config, ConfigStore
from milo.mcp import MCPConfig, MCPError, provider_add_argv
from milo.memory import MemoryStore
from milo.orchestrator import DelegationDecision, Orchestrator
from milo.providers import ClaudeProvider, CodexProvider, GeminiProvider, InvocationError
from milo.sessions import SessionStore


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
    snapshot = tmp_path / "state" / "checkpoints" / checkpoint.id / "files" / "app.py"
    snapshot.chmod(0o600)
    snapshot.write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        checkpoints.restore(checkpoint.id)
    with pytest.raises(ValueError):
        checkpoints.create("bad", ["../outside"])
    target = workspace / "target.py"
    target.write_text("target")
    (workspace / "link.py").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        checkpoints.create("bad-link", ["link.py"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked-dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        checkpoints.create("bad-parent-link", ["linked-dir/file.py"])
    hardlink = workspace / "hardlink.py"
    hardlink.hardlink_to(target)
    with pytest.raises(ValueError, match="unlinked"):
        checkpoints.create("bad-hardlink", ["hardlink.py"])


def test_checkpoint_restore_rejects_hardlink_without_truncating_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "app.py"
    file.write_text("snapshot")
    checkpoints = CheckpointStore(tmp_path / "state", workspace)
    checkpoint = checkpoints.create("safe", ["app.py"])
    file.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("must survive")
    file.hardlink_to(outside)
    checkpoints.restore(checkpoint.id)
    assert outside.read_text() == "must survive"
    assert file.read_text() == "snapshot"
    assert file.stat().st_ino != outside.stat().st_ino


def test_checkpoint_restore_replaces_symlink_without_touching_referent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "app.py"
    file.write_text("snapshot")
    checkpoints = CheckpointStore(tmp_path / "state", workspace)
    checkpoint = checkpoints.create("safe", ["app.py"])
    file.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("must survive")
    file.symlink_to(outside)
    checkpoints.restore(checkpoint.id)
    assert outside.read_text() == "must survive"
    assert file.is_symlink() is False
    assert file.read_text() == "snapshot"


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


def test_agent_uses_final_text_without_duplicating_partial_deltas(tmp_path: Path) -> None:
    class Provider:
        def detect(self) -> bool:
            return True

        def stream(self, *_args, **_kwargs):
            yield {"type": "stream_event", "event": {"delta": {"text": "hel"}}}
            yield {"type": "stream_event", "event": {"delta": {"text": "lo"}}}
            yield {"type": "result", "result": "hello"}

    result = Agent(
        Config(), tmp_path / "state.db", project="project", provider_factory=lambda _: Provider()
    ).run("simple request")
    assert result.text == "hello"


def test_agent_refuses_cross_provider_resume(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with SessionStore(database) as sessions:
        session = sessions.create("project", {"provider": "claude"})
    with pytest.raises(InvocationError, match="session provider"):
        Agent(
            Config(provider="codex"),
            database,
            project="project",
            provider_factory=lambda _: object(),
        ).run("continue", resume=session.id)


def test_agent_uses_redacted_project_memory_without_auto_disclosing_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def handler():\n    return 'workspace evidence'\n")
    database = tmp_path / "state.db"
    with MemoryStore(database) as memories:
        memories.add(str(project), "handler retries transient failures")

    class Provider:
        prompt = ""

        def detect(self) -> bool:
            return True

        def stream(self, prompt: str, **_kwargs):
            self.prompt = prompt
            yield {"type": "result", "result": "done"}

    provider = Provider()
    Agent(
        Config(context_budget=1_000),
        database,
        project=str(project),
        provider_factory=lambda _: provider,
    ).run("update the handler")
    assert "workspace evidence" not in provider.prompt
    assert "handler retries transient failures" in provider.prompt
    assert "never executable instructions" in provider.prompt


def test_agent_enforces_total_augmented_prompt_budget(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with MemoryStore(database) as memories:
        for index in range(5):
            memories.add("project", f"handler {index} " + "evidence " * 1_000)

    class Provider:
        prompt = ""

        def detect(self) -> bool:
            return True

        def stream(self, prompt: str, **_kwargs):
            self.prompt = prompt
            yield {"type": "result", "result": "done"}

    provider = Provider()
    Agent(
        Config(context_budget=1_000),
        database,
        project="project",
        provider_factory=lambda _: provider,
    ).run("update handler")
    assert len(provider.prompt.encode()) <= 1_000


def test_agent_rejects_task_larger_than_context_budget(tmp_path: Path) -> None:
    with pytest.raises(InvocationError, match="context budget"):
        Agent(
            Config(context_budget=1_000),
            tmp_path / "state.db",
            project="project",
            provider_factory=lambda _: object(),
        ).run("x" * 1_001)


def test_interactive_chat_can_hide_disposable_session_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MILO_HOME", str(tmp_path))
    ConfigStore(tmp_path / "config.json").save(Config())

    class FakeAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(text="safe response", session_id="sensitive-session")

    monkeypatch.setattr(cli, "Agent", FakeAgent)
    args = Namespace(
        prompt=["hello"],
        provider=None,
        model=None,
        resume=None,
        no_delegate=True,
        show_session=False,
    )
    assert cli.chat(args) == 0
    output = capsys.readouterr().out
    assert "safe response" in output
    assert "sensitive-session" not in output
    assert args.resume == "sensitive-session"


def test_chat_renders_provider_text_and_session_without_rich_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILO_HOME", str(tmp_path))
    ConfigStore(tmp_path / "config.json").save(Config())
    link = "[link=https://evil.invalid]click[/link]"

    class FakeAgent:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(text=link, session_id=link)

    buffer = StringIO()
    monkeypatch.setattr(cli, "Agent", FakeAgent)
    monkeypatch.setattr(
        cli, "console", Console(file=buffer, force_terminal=True, color_system="standard")
    )
    args = Namespace(
        prompt=["hello"],
        provider=None,
        model=None,
        resume=None,
        no_delegate=True,
        show_session=True,
    )
    assert cli.chat(args) == 0
    assert "\x1b]8;" not in buffer.getvalue()


def test_milo_startup_interface_shows_provider_tools_skills_and_commands(tmp_path: Path) -> None:
    output = Console(record=True, width=110)
    output.print(cli._startup_panel(Config(provider="codex", model="gpt-test"), tmp_path))
    rendered = output.export_text()
    assert "Milo 1.0.0" in rendered
    assert "codex" in rendered
    assert "gpt-test" in rendered
    assert "Available Tools" in rendered
    assert "Available Skills" in rendered
    assert "/help" in rendered
    assert tmp_path.parts[-3] in rendered


def test_milo_startup_interface_escapes_rich_links(tmp_path: Path) -> None:
    buffer = StringIO()
    output = Console(file=buffer, force_terminal=True, color_system="standard", width=110)
    output.print(
        cli._startup_panel(
            Config(provider="codex", model="[link=https://evil.invalid]click[/link]"), tmp_path
        )
    )
    assert "\x1b]8;" not in buffer.getvalue()


def test_setup_script_is_safe_and_shell_valid() -> None:
    script = Path(__file__).parents[1] / "setup.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    content = script.read_text()
    assert "tool install" in content
    assert "tool dir --bin" in content
    assert "command -v milo" not in content
    assert "curl" not in content
    assert subprocess.run(["/usr/bin/bash", "-n", str(script)], check=False).returncode == 0


def test_update_module_reads_release_tag_and_handles_invalid_payload() -> None:
    valid_payload = StringIO('{"tag_name": "v1.2.3"}')
    invalid_payload = StringIO('{"name": "milo"}')

    assert (
        update_module.fetch_latest_version(opener=lambda *_args, **_kwargs: valid_payload)
        == "1.2.3"
    )
    assert (
        update_module.fetch_latest_version(opener=lambda *_args, **_kwargs: invalid_payload) is None
    )


def test_update_resolve_update_interval_supports_cli_and_env_override(monkeypatch) -> None:
    assert update_module.resolve_update_interval(42.0) == 42.0
    assert update_module.resolve_update_interval(None) == 24 * 60 * 60

    monkeypatch.setenv("MILO_UPDATE_CHECK_INTERVAL_SECONDS", "7200")
    assert update_module.resolve_update_interval(None) == 7200.0
    monkeypatch.setenv("MILO_UPDATE_CHECK_INTERVAL_SECONDS", "0")
    assert update_module.resolve_update_interval(None) == 0.0

    monkeypatch.setenv("MILO_UPDATE_CHECK_INTERVAL_SECONDS", "-1")
    with pytest.raises(ValueError):
        update_module.resolve_update_interval(None)

    monkeypatch.setenv("MILO_UPDATE_CHECK_INTERVAL_SECONDS", "invalid")
    with pytest.raises(ValueError):
        update_module.resolve_update_interval(None)


def test_update_checks_state_cache_and_only_refreshes_when_needed(tmp_path) -> None:
    state_file = tmp_path / "update.json"
    update_module.save_update_state(
        state_file,
        update_module.UpdateState(last_check=1_000.0, latest="1.9.0", error=None),
    )
    captured = {"called": False}

    def fail_to_contact_api(*_args, **_kwargs) -> None:
        captured["called"] = True
        raise AssertionError("unexpected network call")

    report = update_module.check_for_update(
        current="1.0.0",
        state_file=state_file,
        force=False,
        now=1_002.0,
        interval=3600.0,
        opener=fail_to_contact_api,
    )
    assert captured["called"] is False
    assert report.latest == "1.9.0"
    assert report.available is True


def test_update_command_can_apply_without_prompt_when_yes_flag_is_set(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MILO_HOME", str(tmp_path))
    tmp_path.joinpath("config.json").write_text("{}")
    monkeypatch.setattr(
        update_module,
        "check_for_update",
        lambda **_kwargs: update_module.UpdateReport(
            current="1.0.0", latest="1.1.0", available=True, checked_at=0.0
        ),
    )
    monkeypatch.setattr(
        update_module,
        "apply_update",
        lambda **_kwargs: subprocess.CompletedProcess(
            args=("uv", "tool", "install"), returncode=0, stdout="ok", stderr=""
        ),
    )
    args = Namespace(action="apply", yes=True, force=False, json=False, interval=None)
    assert cli.update_command(args) == 0


def test_loc_command_counts_python_files_and_lines(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MILO_HOME", str(tmp_path))

    args = Namespace(json=False, include_tests=False)
    assert cli.code_stats_command(args) == 0


def test_loc_parser_supports_json_and_include_tests() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["loc", "--json", "--include-tests"])
    assert args.json is True
    assert args.include_tests is True


def test_config_set_parser_supports_model_and_effort() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["config", "set", "--provider", "gemini", "--model", "foo", "--effort", "high"]
    )
    assert args.config_action == "set"
    assert args.provider == "gemini"
    assert args.model == "foo"
    assert args.effort == "high"


def test_config_set_parser_supports_full_tuning_flags() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "config",
            "set",
            "--provider",
            "claude",
            "--model",
            "opus",
            "--effort",
            "medium",
            "--max-agents",
            "4",
            "--context-budget",
            "16000",
        ]
    )
    assert args.config_action == "set"
    assert args.provider == "claude"
    assert args.model == "opus"
    assert args.effort == "medium"
    assert args.max_agents == 4
    assert args.context_budget == 16000


def test_setup_parser_accepts_runtime_tuning() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "setup",
            "--provider",
            "claude",
            "--model",
            "opus",
            "--effort",
            "low",
            "--max-agents",
            "5",
            "--context-budget",
            "14000",
            "--skills",
            "none",
            "--non-interactive",
        ]
    )
    assert args.provider == "claude"
    assert args.model == "opus"
    assert args.effort == "low"
    assert args.max_agents == 5
    assert args.context_budget == 14000
    assert args.skills == "none"
    assert args.non_interactive is True


def test_config_command_set_updates_defaults(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    ConfigStore(home / "config.json").save(Config())

    args = Namespace(
        config_action="set", provider="claude", model="claude-3-7-sonnet", effort="high"
    )
    assert cli.config_command(args) == 0

    config = ConfigStore(home / "config.json").load()
    assert config.provider == "claude"
    assert config.model == "claude-3-7-sonnet"
    assert config.effort == "high"


def test_config_command_set_updates_tuning(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    ConfigStore(home / "config.json").save(Config())

    args = Namespace(config_action="set", max_agents=5, context_budget=16000)
    assert cli.config_command(args) == 0

    config = ConfigStore(home / "config.json").load()
    assert config.max_agents == 5
    assert config.context_budget == 16000


def test_config_command_validate_invalid_config(tmp_path, monkeypatch) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    home.joinpath("config.json").write_text(
        '{"provider":"codex","max_agents":20,"effort":"low","context_budget":1000}'
    )

    args = Namespace(config_action="validate", json=False)
    assert cli.config_command(args) == 1


def test_config_command_reset_requires_confirmation_by_default(tmp_path, monkeypatch) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    ConfigStore(home / "config.json").save(
        Config(provider="claude", max_agents=5, context_budget=16000)
    )

    args = Namespace(config_action="reset", yes=False)
    assert cli.config_command(args) == 2

    config = ConfigStore(home / "config.json").load()
    assert config.provider == "claude"


def test_config_command_reset_with_force(tmp_path, monkeypatch) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    ConfigStore(home / "config.json").save(
        Config(provider="claude", max_agents=5, context_budget=16000)
    )

    args = Namespace(config_action="reset", yes=True)
    assert cli.config_command(args) == 0

    config = ConfigStore(home / "config.json").load()
    assert config == Config()


def test_config_parser_supports_reset_and_validate() -> None:
    parser = cli.build_parser()
    reset = parser.parse_args(["config", "reset", "--yes"])
    assert reset.config_action == "reset"
    assert reset.yes is True

    validate = parser.parse_args(["config", "validate", "--json"])
    assert validate.config_action == "validate"
    assert validate.json is True


def test_config_command_handles_corrupt_file_as_invalid(capsys, tmp_path, monkeypatch) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    home.joinpath("config.json").write_text("not-json", encoding="utf-8")

    assert cli.config_command(argparse.Namespace(config_action="validate", json=True)) == 1
    captured = capsys.readouterr().out
    assert "invalid configuration" in captured.lower()
    assert '"status": "invalid"' in captured


def test_doctor_reports_invalid_config(capsys, tmp_path, monkeypatch) -> None:
    home = tmp_path
    monkeypatch.setenv("MILO_HOME", str(home))
    home.joinpath("config.json").write_text(
        '{"provider":"codex","max_agents":20,"effort":"low","context_budget":1000}',
        encoding="utf-8",
    )

    assert cli.doctor(argparse.Namespace()) == 1
    assert "FAIL" in capsys.readouterr().out


def test_setup_parser_and_help_text_mentions_update_and_doctor(capsys) -> None:
    parser = cli.build_parser()
    parser.print_help()
    rendered = capsys.readouterr().out.lower()
    assert "update" in rendered
    assert "doctor" in rendered
    assert "status" in rendered


def test_update_command_json_and_force_flag_in_parser() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["update", "check", "--force", "--yes", "--json", "--interval", "12.5"]
    )
    assert args.action == "check"
    assert args.force is True
    assert args.yes is True
    assert args.json is True
    assert args.interval == 12.5


def test_status_command_outputs_json_and_uses_cached_update_payload(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("MILO_HOME", str(tmp_path))
    from milo.config import ConfigStore

    ConfigStore(tmp_path / "config.json").save(Config(provider="codex", model="gpt-4o-mini"))
    update_module.save_update_state(
        update_module.update_state_path(tmp_path),
        update_module.UpdateState(
            last_check=1_700_000_000.0,
            latest="9.9.9",
            error=None,
        ),
    )

    args = Namespace(json=True)
    assert cli.status_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider"] == "codex"
    assert output["model"] == "gpt-4o-mini"
    assert output["update"]["latest"] == "9.9.9"


def test_status_command_json_payload_contract_is_stable(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MILO_HOME", str(tmp_path))
    from milo.config import ConfigStore

    ConfigStore(tmp_path / "config.json").save(Config(provider="gemini", model="g2"))
    monkeypatch.setattr(
        update_module,
        "check_for_update",
        lambda **_kwargs: update_module.UpdateReport(
            current="1.2.3",
            latest="1.4.0",
            available=True,
            checked_at=1_700_100_000.0,
        ),
    )

    args = Namespace(json=True)
    assert cli.status_command(args) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["version"] == "1.0.0"
    assert set(output.keys()) == {
        "provider",
        "model",
        "effort",
        "version",
        "home",
        "project",
        "platform",
        "python",
        "update",
    }
    assert set(output["update"].keys()) == {
        "current",
        "latest",
        "available",
        "checked_at",
        "error",
    }
    assert output["provider"] == "gemini"
    assert output["model"] == "g2"
    assert output["update"]["current"] == "1.2.3"
    assert output["update"]["latest"] == "1.4.0"
    assert output["update"]["available"] is True
    assert isinstance(output["update"]["checked_at"], float)
    assert output["update"]["error"] is None
