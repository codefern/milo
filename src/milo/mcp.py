from __future__ import annotations

import builtins
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .research import ResearchError, validate_public_url


class MCPError(ValueError):
    """An MCP server definition violates Milo's extension policy."""


_SAFE_ENV = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR"}
_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9._+-]+$")
_BLOCKED = {"bash", "sh", "zsh", "fish", "sudo", "su", "eval"}


@dataclass(frozen=True)
class MCPConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    timeout: int = 120

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPConfig:
        name, command, url = data.get("name"), data.get("command"), data.get("url")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise MCPError("invalid MCP server name")
        if bool(command) == bool(url):
            raise MCPError("exactly one of command or url is required")
        if command and (
            not isinstance(command, str)
            or not _SAFE_COMMAND.fullmatch(command)
            or command in _BLOCKED
        ):
            raise MCPError("unsafe MCP command")
        if url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise MCPError("remote MCP URLs must use HTTPS")
            try:
                validate_public_url(url)
            except ResearchError as exc:
                raise MCPError(str(exc)) from exc
        args = data.get("args", [])
        env = data.get("env", {})
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise MCPError("MCP args must be strings")
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise MCPError("MCP env must be a string map")
        timeout = data.get("timeout", 120)
        if not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise MCPError("MCP timeout must be 1..600 seconds")
        return cls(name, command, tuple(args), url, tuple(env.items()), timeout)

    def subprocess_environment(self, source: Mapping[str, str]) -> dict[str, str]:
        inherited = {
            key: value
            for key, value in source.items()
            if key in _SAFE_ENV or key.startswith("XDG_")
        }
        inherited.update(dict(self.env))
        return inherited


def provider_add_argv(provider: str, config: MCPConfig) -> list[str]:
    """Build an official provider CLI command for an explicit MCP sync."""
    target = config.url or config.command
    if target is None:
        raise MCPError("MCP target is missing")
    if provider == "codex":
        if config.url:
            return ["codex", "mcp", "add", config.name, "--url", target]
        return ["codex", "mcp", "add", config.name, "--", target, *config.args]
    if provider == "claude":
        if config.url:
            return [
                "claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "--transport",
                "http",
                config.name,
                target,
            ]
        return [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            config.name,
            "--",
            target,
            *config.args,
        ]
    if provider == "gemini":
        transport = "http" if config.url else "stdio"
        return [
            "gemini",
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            transport,
            config.name,
            target,
            *config.args,
        ]
    raise MCPError(f"unsupported provider: {provider}")


def provider_remove_argv(provider: str, name: str) -> list[str]:
    if provider == "codex":
        return ["codex", "mcp", "remove", name]
    if provider in {"claude", "gemini"}:
        return [provider, "mcp", "remove", "--scope", "user", name]
    raise MCPError(f"unsupported provider: {provider}")


class MCPStore:
    """Inspectable MCP configuration; providers execute servers in their own sandbox."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def list(self) -> builtins.list[MCPConfig]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise MCPError("MCP configuration must be a list")
        return [MCPConfig.from_dict(item) for item in value]

    def save(self, configs: builtins.list[MCPConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        values = [
            {
                "name": item.name,
                "command": item.command,
                "args": list(item.args),
                "url": item.url,
                "env": dict(item.env),
                "timeout": item.timeout,
            }
            for item in configs
        ]
        self.path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.path, 0o600)

    def add(self, config: MCPConfig) -> None:
        self.save([item for item in self.list() if item.name != config.name] + [config])

    def remove(self, name: str) -> bool:
        configs = self.list()
        remaining = [item for item in configs if item.name != name]
        if len(configs) == len(remaining):
            return False
        self.save(remaining)
        return True
