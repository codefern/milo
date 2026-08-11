"""Inspectable, permissioned, and auditable tool primitives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from os import PathLike
from pathlib import Path
from typing import Any

from .context import ContextSelector
from .research import fetch_source
from .security import CommandPolicy, PathPolicy, SecurityError, redact_secrets

JSON = dict[str, Any]


class Permission(StrEnum):
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    EXECUTE = "execute"
    NETWORK = "network"
    GIT_WRITE = "git_write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JSON
    permission: Permission
    module: str = "custom"

    def __post_init__(self) -> None:
        if not self.name or self.input_schema.get("type") != "object":
            raise ValueError("tool name and object input schema are required")

    def as_dict(self) -> JSON:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "permission": self.permission.value,
            "module": self.module,
        }


class AuditLog:
    """Emit JSON-like tool events to a caller-controlled sink."""

    def __init__(self, sink: Callable[[JSON], object]) -> None:
        self._sink = sink

    def record(
        self, *, tool: str, arguments: Mapping[str, Any], outcome: str, error: str | None = None
    ) -> None:
        event: JSON = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "arguments": redact_secrets(dict(arguments)),
            "outcome": outcome,
        }
        if error is not None:
            event["error"] = redact_secrets(error)
        self._sink(event)


def _validate(schema: Mapping[str, Any], value: Any, location: str = "arguments") -> None:
    expected = schema.get("type")
    valid = (
        expected == "object"
        and isinstance(value, dict)
        or expected == "array"
        and isinstance(value, list)
        or expected == "string"
        and isinstance(value, str)
        or expected == "integer"
        and isinstance(value, int)
        and not isinstance(value, bool)
        or expected == "boolean"
        and isinstance(value, bool)
        or expected == "number"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    )
    if expected in {"object", "array", "string", "integer", "boolean", "number"} and not valid:
        raise ValueError(f"{location} must be {expected}")
    if expected == "object":
        assert isinstance(value, dict)
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                raise ValueError(f"missing required argument: {required}")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise ValueError(f"unexpected argument: {sorted(extras)[0]}")
        for key, item in value.items():
            if key in properties:
                _validate(properties[key], item, key)
    elif expected == "array" and "items" in schema:
        assert isinstance(value, list)
        for index, item in enumerate(value):
            _validate(schema["items"], item, f"{location}[{index}]")


class Tool:
    def __init__(
        self,
        spec: ToolSpec,
        handler: Callable[[JSON], Any],
        *,
        audit: AuditLog | None = None,
    ) -> None:
        self.spec = spec
        self._handler = handler
        self._audit = audit

    def invoke(
        self, arguments: JSON, *, permissions: set[Permission] | frozenset[Permission]
    ) -> Any:
        try:
            if self.spec.permission not in permissions:
                raise SecurityError(f"missing permission: {self.spec.permission.value}")
            _validate(self.spec.input_schema, arguments)
            result = self._handler(arguments)
        except Exception as exc:
            if self._audit:
                self._audit.record(
                    tool=self.spec.name, arguments=arguments, outcome="error", error=str(exc)
                )
            raise
        if self._audit:
            self._audit.record(tool=self.spec.name, arguments=arguments, outcome="success")
        return result


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def list_specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools)]

    def invoke(self, name: str, arguments: JSON, *, permissions: set[Permission]) -> Any:
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name].invoke(arguments, permissions=permissions)

    @classmethod
    def builtins(
        cls,
        *,
        workspace: str | PathLike[str],
        command_policy: CommandPolicy,
        command_runner: Callable[[tuple[str, ...], Path], Any],
        path_policy: PathPolicy | None = None,
        audit: AuditLog | None = None,
    ) -> ToolRegistry:
        policy = path_policy or PathPolicy(workspace)
        registry = cls()
        specs = {spec.name: spec for spec in builtin_tool_specs()}

        def run(args: JSON) -> Any:
            argv = command_policy.authorize(args["argv"], approved=False)
            cwd = policy.authorize(args.get("cwd", "."))
            return command_runner(argv, cwd)

        def read(args: JSON) -> JSON:
            path = policy.authorize(args["path"])
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}

        def write(args: JSON) -> JSON:
            path = policy.authorize(args["path"], write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return {"path": str(path), "bytes": len(args["content"].encode())}

        def command_handler(prefix: tuple[str, ...]) -> Callable[[JSON], Any]:
            def handler(args: JSON) -> Any:
                argv = command_policy.authorize(prefix + tuple(args["argv"]), approved=False)
                return command_runner(argv, policy.authorize(args.get("cwd", ".")))

            return handler

        registry.register(Tool(specs["terminal.run"], run, audit=audit))
        registry.register(Tool(specs["files.read"], read, audit=audit))
        registry.register(Tool(specs["files.write"], write, audit=audit))
        registry.register(Tool(specs["git.run"], command_handler(("git",)), audit=audit))
        registry.register(Tool(specs["github.run"], command_handler(("gh",)), audit=audit))

        def web_fetch(args: JSON) -> JSON:
            source = fetch_source(args["url"])
            return {"url": source.url, "title": source.title, "content": source.content}

        def code_search(args: JSON) -> JSON:
            result = ContextSelector(workspace).select(
                keywords=args["query"].split(), token_budget=4_000
            )
            return {
                "matches": [
                    {"path": str(item.path), "content": item.content, "score": item.score}
                    for item in result.items
                ],
                "used_tokens": result.used_tokens,
            }

        registry.register(Tool(specs["web.fetch"], web_fetch, audit=audit))
        registry.register(Tool(specs["code.search"], code_search, audit=audit))
        return registry


def _object(properties: JSON, required: list[str]) -> JSON:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def builtin_tool_specs() -> list[ToolSpec]:
    argv = {"type": "array", "items": {"type": "string"}}
    cwd = {"type": "string"}

    return [
        ToolSpec(
            "terminal.run",
            "Run explicit argv",
            _object({"argv": argv, "cwd": cwd}, ["argv"]),
            Permission.EXECUTE,
            "terminal",
        ),
        ToolSpec(
            "files.read",
            "Read workspace file",
            _object({"path": {"type": "string"}}, ["path"]),
            Permission.READ_FILES,
            "files",
        ),
        ToolSpec(
            "files.write",
            "Write workspace file",
            _object(
                {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]
            ),
            Permission.WRITE_FILES,
            "files",
        ),
        ToolSpec(
            "git.run",
            "Run git argv",
            _object({"argv": argv, "cwd": cwd}, ["argv"]),
            Permission.GIT_WRITE,
            "git",
        ),
        ToolSpec(
            "github.run",
            "Run GitHub CLI argv",
            _object({"argv": argv, "cwd": cwd}, ["argv"]),
            Permission.NETWORK,
            "github",
        ),
        ToolSpec(
            "web.fetch",
            "Fetch a public HTTPS source",
            _object({"url": {"type": "string"}}, ["url"]),
            Permission.NETWORK,
            "web",
        ),
        ToolSpec(
            "code.search",
            "Search relevant workspace code",
            _object({"query": {"type": "string"}}, ["query"]),
            Permission.READ_FILES,
            "code",
        ),
    ]
