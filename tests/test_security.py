from pathlib import Path

import pytest

from milo.security import (
    CommandPolicy,
    PathPolicy,
    PolicyDecision,
    RiskLevel,
    SecurityError,
    redact_secrets,
)


def test_redact_secrets_recursively_without_mutating_input() -> None:
    value = {
        "Authorization": "Bearer abc",
        "nested": {"api_token": "secret", "safe": "visible"},
        "items": [{"password": "hunter2"}],
    }

    redacted = redact_secrets(value)

    assert redacted == {
        "Authorization": "[REDACTED]",
        "nested": {"api_token": "[REDACTED]", "safe": "visible"},
        "items": [{"password": "[REDACTED]"}],
    }
    assert value["nested"]["api_token"] == "secret"
    assert (
        redact_secrets("request failed with Bearer abc.def-123")
        == "request failed with Bearer [REDACTED]"
    )
    assert redact_secrets("token ghp_abcdefghijklmnopqrstuvwxyz123456") == "token [REDACTED]"


def test_path_policy_rejects_escape_and_protected_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = PathPolicy(workspace)

    assert (
        policy.authorize(workspace / "file.txt", write=True) == (workspace / "file.txt").resolve()
    )
    with pytest.raises(SecurityError):
        policy.authorize(workspace / ".." / "escape", write=True)
    with pytest.raises(SecurityError):
        policy.authorize("/opt/va-backend/config", write=True)


def test_command_policy_requires_argv_and_returns_structured_risk() -> None:
    policy = CommandPolicy(allowed_commands={"git", "python"})

    allowed = policy.evaluate(["git", "rev-parse", "HEAD"])
    denied_shell = policy.evaluate("git status")
    approval = policy.evaluate(["git", "push", "origin", "main"])

    assert allowed == PolicyDecision(True, RiskLevel.LOW, "allowed")
    assert denied_shell.allowed is False
    assert denied_shell.reason == "commands must be an argv sequence; shell execution is disabled"
    assert approval.allowed is False
    assert approval.requires_approval is True
    assert approval.risk is RiskLevel.HIGH


def test_command_policy_blocks_git_alias_shell_escape() -> None:
    policy = CommandPolicy(allowed_commands={"git"})
    assert policy.evaluate(["git", "-c", "alias.pwn=!id", "pwn"]).allowed is False
    assert policy.evaluate(["git", "config", "alias.pwn", "!id"]).allowed is False
    assert policy.evaluate(["git", "grep", "--open-files-in-pager=id", "x"]).allowed is False
    assert policy.evaluate(["git", "show", "--ext-diff", "HEAD"]).allowed is False
    assert policy.evaluate(["git", "diff"]).requires_approval is True
    assert policy.authorize(["git", "rev-parse", "HEAD"])[1:5] == (
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
    )
