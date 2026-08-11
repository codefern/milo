from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .agent import Agent
from .automation import AutomationStore
from .checkpoints import CheckpointStore
from .config import PROVIDERS, Config, ConfigStore
from .mcp import MCPConfig, MCPError, MCPStore, provider_add_argv, provider_remove_argv
from .memory import MemoryStore
from .providers import UnsupportedCapabilityError, get_provider
from .security import redact_secrets
from .sessions import SessionStore
from .skills import SkillError, SkillInstaller, load_catalog
from .storage import resolve_milo_home

console = Console()


def _home() -> Path:
    return resolve_milo_home()


def _config_store() -> ConfigStore:
    return ConfigStore(_home() / "config.json")


def _catalog() -> Path:
    return Path(str(resources.files("milo").joinpath("catalog", "skills")))


def _catalog_metadata() -> dict[str, object]:
    value: Any = json.loads(resources.files("milo").joinpath("catalog", "catalog.json").read_text())
    return cast(dict[str, object], value)


def _provider_status(name: str) -> tuple[bool, str]:
    provider = get_provider(name)
    if not provider.detect():
        return False, "not installed"
    try:
        authenticated = provider.is_authenticated()
        return authenticated, "authenticated" if authenticated else "authentication required"
    except UnsupportedCapabilityError:
        return True, "installed; authentication is verified by Gemini on invocation"


def _install_hint(name: str) -> str:
    return {
        "codex": "npm install -g @openai/codex",
        "claude": "npm install -g @anthropic-ai/claude-code",
        "gemini": "npm install -g @google/gemini-cli",
    }[name]


def setup(args: argparse.Namespace) -> int:
    console.print(
        Panel.fit("[bold cyan]Milo setup[/]\nProvider-native. Credential-safe. Recoverable.")
    )
    table = Table("Provider", "CLI", "Authentication")
    statuses: dict[str, tuple[bool, str]] = {}
    for name in PROVIDERS:
        status = _provider_status(name)
        statuses[name] = status
        table.add_row(name, "found" if get_provider(name).detect() else "missing", status[1])
    console.print(table)
    provider_name = args.provider
    if provider_name is None:
        if not sys.stdin.isatty():
            provider_name = next((name for name, status in statuses.items() if status[0]), "codex")
        else:
            entered = console.input("Provider [codex/claude/gemini] [codex]: ").strip().lower()
            provider_name = entered or "codex"
    if provider_name not in PROVIDERS:
        console.print(f"[red]Unknown provider:[/] {provider_name}")
        return 2
    provider = get_provider(provider_name)
    if not provider.detect():
        console.print(
            f"[yellow]{provider_name} is missing.[/] Official install: {_install_hint(provider_name)}"
        )
        return 2
    try:
        authenticated = provider.is_authenticated()
    except UnsupportedCapabilityError:
        authenticated = True
    if not authenticated:
        if args.non_interactive:
            console.print(
                f"[yellow]Authentication required.[/] Run: {' '.join(provider.login_argv)}"
            )
            return 2
        console.print(
            "Starting the provider's official authentication flow; Milo never reads the credential."
        )
        provider.login()
        if not provider.is_authenticated():
            console.print("[red]Authentication could not be verified.[/]")
            return 2

    config = Config(provider=provider_name, model=args.model)
    _config_store().save(config)
    skill_choice = args.skills
    if skill_choice is None and sys.stdin.isatty():
        choice = console.input("Skills [recommended/all/none] [recommended]: ").strip().lower()
        skill_choice = choice or "recommended"
    skill_choice = skill_choice or "recommended"
    installed = 0
    if skill_choice != "none":
        metadata = cast(list[dict[str, Any]], _catalog_metadata()["skills"])
        names = [item["name"] for item in metadata if skill_choice == "all" or item["recommended"]]
        console.print(
            f"[bold]Skills selected ({len(names)}):[/] " + ", ".join(str(name) for name in names)
        )
        if skill_choice == "all" and sys.stdin.isatty() and not args.non_interactive:
            confirmed = console.input("Install the complete catalog? [y/N]: ").strip().lower()
            if confirmed not in {"y", "yes"}:
                names = []
                console.print("Full-catalog installation cancelled.")
        installer = SkillInstaller(_home() / "skills", milo_version=__version__)
        for name in names:
            source = _catalog() / str(name)
            try:
                if (_home() / "skills" / str(name)).exists():
                    installer.update(source)
                else:
                    installer.install(source)
                installed += 1
            except SkillError as exc:
                console.print(f"[yellow]Skill {name} failed:[/] {exc}")
    console.print(
        Panel.fit(
            f"[bold green]Setup complete[/]\nProvider: {provider_name}\nSkills installed: {installed}\nState: {_home()}"
        )
    )
    return 0


def doctor(_args: argparse.Namespace) -> int:
    table = Table("Check", "Status", "Detail")
    failures = 0
    for name in PROVIDERS:
        ok, detail = _provider_status(name)
        table.add_row(name, "OK" if ok else "WARN", detail)
    try:
        config = _config_store().load()
        table.add_row("configuration", "OK", f"provider={config.provider}")
    except ValueError as exc:
        failures += 1
        table.add_row("configuration", "FAIL", str(exc))
    table.add_row("state permissions", "OK", oct(_home().stat().st_mode & 0o777))
    table.add_row(
        "platform",
        "OK",
        f"{platform.system()} {platform.machine()} / Python {platform.python_version()}",
    )
    console.print(table)
    return 1 if failures else 0


def chat(args: argparse.Namespace) -> int:
    if not (_home() / "config.json").exists():
        code = setup(
            argparse.Namespace(
                provider=args.provider,
                model=args.model,
                skills="recommended",
                non_interactive=not sys.stdin.isatty(),
            )
        )
        if code:
            return code
    config = _config_store().load()
    if args.provider:
        config = replace(config, provider=args.provider)
    if args.model:
        config = replace(config, model=args.model)
    task = " ".join(args.prompt).strip()
    if not task:
        console.print(
            "[red]A prompt is required. Use `milo chat <prompt>` or run `milo` interactively.[/]"
        )
        return 2
    try:
        streamed: list[str] = []

        def show_stream(text: str) -> None:
            streamed.append(text)
            console.print(text, end="")

        def show_delegation(decision: Any) -> None:
            console.print(f"[dim]Sub-agents: {', '.join(decision.roles)}[/]")

        result = Agent(
            config,
            _home() / "state.db",
            project=str(Path.cwd().resolve()),
            on_text=show_stream,
            on_delegation=show_delegation,
        ).run(task, resume=args.resume, delegate=False if args.no_delegate else None)
    except Exception as exc:
        console.print(f"[red]Milo could not complete the task:[/] {exc}")
        return 1
    if streamed:
        console.print()
    else:
        console.print(result.text)
    console.print(f"[dim]Session: {result.session_id}[/]")
    return 0


def skills_command(args: argparse.Namespace) -> int:
    installer = SkillInstaller(_home() / "skills", milo_version=__version__)
    if args.skill_action in {"catalog", "list"}:
        installed = {item.name for item in installer.list_installed()}
        table = Table("Skill", "Recommended", "Status", "Description")
        for item in cast(list[dict[str, Any]], _catalog_metadata()["skills"]):
            table.add_row(
                item["name"],
                "yes" if item["recommended"] else "",
                "installed" if item["name"] in installed else "available",
                item["description"],
            )
        console.print(table)
    elif args.skill_action == "install":
        source = _catalog() / args.name
        path = installer.install(source)
        console.print(f"Installed and verified {args.name} at {path}")
    elif args.skill_action == "remove":
        console.print("Removed" if installer.remove(args.name) else "Not installed")
    elif args.skill_action == "validate":
        manifests = load_catalog(_home() / "skills", milo_version=__version__)
        console.print(f"Validated {len(manifests)} installed skills")
    return 0


def memory_command(args: argparse.Namespace) -> int:
    with MemoryStore(_home() / "state.db") as store:
        project = str(Path.cwd().resolve())
        if args.memory_action == "add":
            console.print(store.add(project, args.content))
        elif args.memory_action == "search":
            for item in store.search(project, args.query):
                console.print(f"{item.id}: {item.content}")
        elif args.memory_action == "list":
            for item in store.list(project):
                console.print(f"{item.id}: {item.content}")
        elif args.memory_action == "remove":
            console.print("Removed" if store.remove(args.id) else "Not found")
    return 0


def sessions_command(args: argparse.Namespace) -> int:
    with SessionStore(_home() / "state.db") as store:
        if args.sessions_action == "list":
            for session in store.list(str(Path.cwd().resolve())):
                console.print(
                    f"{session.id}  {session.updated_at.isoformat()}  {len(session.messages)} messages"
                )
        elif args.sessions_action == "show":
            selected = store.get(args.id)
            if not selected:
                console.print("Session not found")
                return 1
            for message in selected.messages:
                console.print(f"[bold]{message.role}:[/] {message.content}")
        elif args.sessions_action == "remove":
            console.print("Removed" if store.delete(args.id) else "Not found")
        elif args.sessions_action == "search":
            project = str(Path.cwd().resolve())
            for selected in store.search(args.query, project=project):
                console.print(f"{selected.id}  {selected.updated_at.isoformat()}")
    return 0


def checkpoint_command(args: argparse.Namespace) -> int:
    store = CheckpointStore(_home(), Path.cwd())
    if args.checkpoint_action == "create":
        checkpoint = store.create(args.label, args.files)
        console.print(checkpoint.id)
    elif args.checkpoint_action == "list":
        for item in store.list():
            console.print(f"{item.id}  {item.label}  {len(item.files)} files")
    elif args.checkpoint_action == "restore":
        console.print(f"Restored {len(store.restore(args.id).files)} files")
    return 0


def automation_command(args: argparse.Namespace) -> int:
    store = AutomationStore(_home() / "automation.json")
    if args.automation_action == "add":
        job = store.create(args.name, args.schedule, args.prompt)
        console.print(
            f"Created paused automation {job.id}; enable it explicitly with `milo automation enable {job.id}`"
        )
    elif args.automation_action == "list":
        for job in store.list():
            console.print(
                f"{job.id}  {'enabled' if job.enabled else 'paused'}  {job.schedule}  {job.name}"
            )
    elif args.automation_action in {"enable", "pause"}:
        console.print(store.set_enabled(args.id, args.automation_action == "enable"))
    elif args.automation_action == "remove":
        console.print("Removed" if store.remove(args.id) else "Not found")
    return 0


def mcp_command(args: argparse.Namespace) -> int:
    store = MCPStore(_home() / "mcp.json")
    if args.mcp_action == "list":
        for config in store.list():
            detail = config.url or f"{config.command} {' '.join(config.args)}"
            console.print(f"{config.name}  {detail}")
    elif args.mcp_action == "add-command":
        store.add(
            MCPConfig.from_dict({"name": args.name, "command": args.executable, "args": args.args})
        )
        console.print(
            f"Added MCP server {args.name}; activate it explicitly with `milo mcp sync {args.name}`"
        )
    elif args.mcp_action == "add-url":
        store.add(MCPConfig.from_dict({"name": args.name, "url": args.url}))
        console.print(
            f"Added MCP server {args.name}; activate it explicitly with `milo mcp sync {args.name}`"
        )
    elif args.mcp_action == "sync":
        selected = next((item for item in store.list() if item.name == args.name), None)
        if selected is None:
            raise MCPError(f"MCP server not found: {args.name}")
        provider = ConfigStore(_home() / "config.json").load().provider
        result = subprocess.run(
            provider_add_argv(provider, selected), capture_output=True, text=True
        )
        if result.returncode:
            error = redact_secrets(result.stderr.strip())
            raise MCPError(str(error or "provider rejected the MCP configuration"))
        console.print(f"Synced {args.name} to {provider}; restart active provider sessions")
    elif args.mcp_action == "unsync":
        provider = ConfigStore(_home() / "config.json").load().provider
        result = subprocess.run(
            provider_remove_argv(provider, args.name), capture_output=True, text=True
        )
        if result.returncode:
            error = redact_secrets(result.stderr.strip())
            raise MCPError(str(error or "provider could not remove the MCP configuration"))
        console.print(f"Removed {args.name} from {provider}")
    elif args.mcp_action == "remove":
        console.print("Removed" if store.remove(args.name) else "Not found")
    return 0


def interactive() -> int:
    if not (_home() / "config.json").exists():
        code = setup(
            argparse.Namespace(provider=None, model=None, skills=None, non_interactive=False)
        )
        if code:
            return code
    console.print(
        Panel.fit("[bold cyan]Milo[/] — provider-native agent\nType /help, /exit, or a task.")
    )
    history_path = _home() / "history"
    history_path.touch(mode=0o600, exist_ok=True)
    os.chmod(history_path, 0o600)
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def submit_multiline(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(["/help", "/exit", "/quit"], sentence=True),
        multiline=True,
        key_bindings=bindings,
        bottom_toolbar="Enter: newline · Esc+Enter: submit · Ctrl+C: cancel",
    )
    while True:
        try:
            prompt = session.prompt("milo› ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if prompt in {"/exit", "/quit"}:
            return 0
        if prompt == "/help":
            console.print("/help /exit — or enter any task. Complex tasks delegate automatically.")
            continue
        if prompt:
            chat(
                argparse.Namespace(
                    prompt=[prompt], provider=None, model=None, resume=None, no_delegate=False
                )
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milo", description="Provider-native AI agent CLI")
    parser.add_argument("--version", action="version", version=f"Milo {__version__}")
    sub = parser.add_subparsers(dest="command")
    setup_parser = sub.add_parser("setup", help="configure a provider and skills")
    setup_parser.add_argument("--provider", choices=PROVIDERS)
    setup_parser.add_argument("--model")
    setup_parser.add_argument("--skills", choices=("recommended", "all", "none"))
    setup_parser.add_argument("--non-interactive", action="store_true")
    sub.add_parser("doctor", help="diagnose installation")
    chat_parser = sub.add_parser("chat", help="run a task")
    chat_parser.add_argument("prompt", nargs="*")
    chat_parser.add_argument("--provider", choices=PROVIDERS)
    chat_parser.add_argument("--model")
    chat_parser.add_argument("--resume")
    chat_parser.add_argument("--no-delegate", action="store_true")
    skill_parser = sub.add_parser("skills", help="manage validated skills")
    skill_sub = skill_parser.add_subparsers(dest="skill_action", required=True)
    skill_sub.add_parser("list")
    skill_sub.add_parser("catalog")
    skill_sub.add_parser("validate")
    for action in ("install", "remove"):
        item = skill_sub.add_parser(action)
        item.add_argument("name")
    memory_parser = sub.add_parser("memory", help="manage project memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_action", required=True)
    add = memory_sub.add_parser("add")
    add.add_argument("content")
    search = memory_sub.add_parser("search")
    search.add_argument("query")
    memory_sub.add_parser("list")
    remove = memory_sub.add_parser("remove")
    remove.add_argument("id", type=int)
    sessions_parser = sub.add_parser("sessions", help="inspect sessions")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_action", required=True)
    sessions_sub.add_parser("list")
    search_session = sessions_sub.add_parser("search")
    search_session.add_argument("query")
    for action in ("show", "remove"):
        item = sessions_sub.add_parser(action)
        item.add_argument("id")
    checkpoints = sub.add_parser("checkpoint", help="create and restore file checkpoints")
    checkpoint_sub = checkpoints.add_subparsers(dest="checkpoint_action", required=True)
    create = checkpoint_sub.add_parser("create")
    create.add_argument("label")
    create.add_argument("files", nargs="+")
    checkpoint_sub.add_parser("list")
    restore = checkpoint_sub.add_parser("restore")
    restore.add_argument("id")
    automation = sub.add_parser("automation", help="manage paused-by-default automations")
    automation_sub = automation.add_subparsers(dest="automation_action", required=True)
    add_job = automation_sub.add_parser("add")
    add_job.add_argument("name")
    add_job.add_argument("schedule")
    add_job.add_argument("prompt")
    automation_sub.add_parser("list")
    for action in ("enable", "pause", "remove"):
        item = automation_sub.add_parser(action)
        item.add_argument("id")
    mcp = sub.add_parser("mcp", help="manage permission-scoped MCP servers")
    mcp_sub = mcp.add_subparsers(dest="mcp_action", required=True)
    mcp_sub.add_parser("list")
    add_command = mcp_sub.add_parser("add-command")
    add_command.add_argument("name")
    add_command.add_argument("executable")
    add_command.add_argument("args", nargs="*")
    add_url = mcp_sub.add_parser("add-url")
    add_url.add_argument("name")
    add_url.add_argument("url")
    remove_mcp = mcp_sub.add_parser("remove")
    remove_mcp.add_argument("name")
    for action in ("sync", "unsync"):
        item = mcp_sub.add_parser(action)
        item.add_argument("name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return interactive()
    handlers = {
        "setup": setup,
        "doctor": doctor,
        "chat": chat,
        "skills": skills_command,
        "memory": memory_command,
        "sessions": sessions_command,
        "checkpoint": checkpoint_command,
        "automation": automation_command,
        "mcp": mcp_command,
    }
    try:
        return handlers[args.command](args)
    except (ValueError, KeyError, OSError, SkillError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
