from pathlib import Path

import pytest

from milo.security import CommandPolicy, PathPolicy, SecurityError
from milo.tools import AuditLog, Permission, Tool, ToolRegistry, ToolSpec, builtin_tool_specs


def test_tool_has_json_schema_permission_boundary_and_redacted_audit(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    audit = AuditLog(events.append)
    spec = ToolSpec(
        name="web.fetch",
        description="Fetch a URL",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}, "api_token": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        permission=Permission.NETWORK,
    )
    tool = Tool(spec, lambda args: {"url": args["url"], "status": 200}, audit=audit)

    with pytest.raises(SecurityError):
        tool.invoke({"url": "https://example.test"}, permissions=set())
    result = tool.invoke(
        {"url": "https://example.test", "api_token": "secret"},
        permissions={Permission.NETWORK},
    )

    assert result["status"] == 200
    assert events[-1]["arguments"] == {
        "url": "https://example.test",
        "api_token": "[REDACTED]",
    }
    assert events[-1]["outcome"] == "success"


def test_registry_rejects_invalid_arguments_and_duplicate_tools() -> None:
    spec = ToolSpec(
        "code.search",
        "Search code",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        Permission.READ_FILES,
    )
    registry = ToolRegistry()
    registry.register(Tool(spec, lambda args: []))

    with pytest.raises(ValueError, match="query"):
        registry.invoke("code.search", {}, permissions={Permission.READ_FILES})
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Tool(spec, lambda args: []))


def test_builtin_tools_are_modular_and_terminal_uses_argv_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    specs = builtin_tool_specs()

    assert {spec.module for spec in specs} == {"terminal", "files", "git", "github", "web", "code"}
    assert all(spec.input_schema["type"] == "object" for spec in specs)

    executed: list[tuple[str, ...]] = []
    registry = ToolRegistry.builtins(
        workspace=workspace,
        command_policy=CommandPolicy(allowed_commands={"git"}),
        command_runner=lambda argv, cwd: executed.append(argv) or {"returncode": 0},
        path_policy=PathPolicy(workspace),
    )
    registry.invoke(
        "terminal.run",
        {"argv": ["git", "rev-parse", "HEAD"]},
        permissions={Permission.EXECUTE},
    )
    assert executed == [
        (
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "HEAD",
        )
    ]
    with pytest.raises((SecurityError, ValueError)):
        registry.invoke(
            "terminal.run",
            {"argv": "git status"},
            permissions={Permission.EXECUTE},
        )
    with pytest.raises(ValueError, match="approved"):
        registry.invoke(
            "git.run",
            {"argv": ["push"], "approved": True},
            permissions={Permission.GIT_WRITE},
        )
    with pytest.raises(SecurityError, match="missing permission"):
        registry.invoke(
            "git.run",
            {"argv": ["status"]},
            permissions={Permission.EXECUTE},
        )
