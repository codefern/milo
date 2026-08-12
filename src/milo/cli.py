from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, replace
from importlib import resources
from pathlib import Path
from typing import Any, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from . import update as update_service
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
from .tools import builtin_tool_specs

console = Console()

_MILO_BANNER = """[bold cyan]
███╗   ███╗██╗██╗      ██████╗
████╗ ████║██║██║     ██╔═══██╗
██╔████╔██║██║██║     ██║   ██║
██║╚██╔╝██║██║██║     ██║   ██║
██║ ╚═╝ ██║██║███████╗╚██████╔╝
╚═╝     ╚═╝╚═╝╚══════╝ ╚═════╝[/]"""


def _startup_panel(config: Config, cwd: Path) -> Panel:
    """Build Milo's full provider-native startup surface."""
    tool_modules = sorted({spec.module for spec in builtin_tool_specs()})
    manifests = load_catalog(_catalog(), milo_version=__version__)
    skill_names = [manifest.name for manifest in manifests]
    model = config.model or "provider default"
    left = (
        _MILO_BANNER
        + f"\n\n[bold]{escape(model)}[/] · {escape(config.provider)}"
        + f"\n[dim]{escape(str(cwd))}[/]"
        + f"\n[dim]Provider-owned authentication · {escape(config.effort)} effort[/]"
    )
    right = (
        "[bold]Available Tools[/]\n"
        + ", ".join(escape(module) for module in tool_modules)
        + f"\n[dim]{len(builtin_tool_specs())} tools · permission scoped[/]"
        + "\n\n[bold]Available Skills[/]\n"
        + ", ".join(escape(name) for name in skill_names[:8])
        + (f", +{len(skill_names) - 8} more" if len(skill_names) > 8 else "")
        + f"\n[dim]{len(skill_names)} validated catalog skills[/]"
        + "\n\n[bold]Session Commands[/]\n"
        + "/help · /new · /retry · /clear · /status · /sessions · /skills · /memory · /doctor · /update · /config · /exit"
    )
    layout = Table.grid(expand=True, padding=(0, 2))
    layout.add_column(ratio=2)
    layout.add_column(ratio=3)
    layout.add_row(left, right)
    return Panel(
        layout,
        title=f"[bold]Milo {__version__} · provider-native agent[/]",
        border_style="cyan",
        padding=(1, 2),
    )


def _home() -> Path:
    return resolve_milo_home()


def _config_store() -> ConfigStore:
    return ConfigStore(_home() / "config.json")


def _catalog() -> Path:
    return Path(str(resources.files("milo").joinpath("catalog", "skills")))


def _catalog_metadata() -> dict[str, object]:
    value: Any = json.loads(resources.files("milo").joinpath("catalog", "catalog.json").read_text())
    return cast(dict[str, object], value)


def _check_update_availability(*, show_only_when_available: bool = True) -> None:
    report = update_service.check_for_update(
        state_file=update_service.update_state_path(_home()),
        force=False,
    )
    if report.error:
        if not show_only_when_available:
            console.print(Text(f"Update check failed: {report.error}", style="yellow"))
        return
    if report.available:
        console.print(
            Text(
                f"Update available: {report.current} -> {report.latest}",
                style="yellow",
            )
        )
        console.print("Run: [yellow]milo update apply[/]", style="dim")
    elif not show_only_when_available:
        console.print(f"Milo is up to date ({report.current}).")


def _status_payload(config: Config) -> dict[str, object]:
    report = update_service.check_for_update(
        current=__version__,
        state_file=update_service.update_state_path(_home()),
        force=False,
    )
    status: dict[str, object] = {
        "provider": config.provider,
        "model": config.model,
        "effort": config.effort,
        "version": __version__,
        "home": str(_home()),
        "project": str(Path.cwd().resolve()),
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "update": {
            "current": report.current,
            "latest": report.latest,
            "available": report.available,
            "checked_at": report.checked_at,
            "error": report.error,
        },
    }
    return status


def _render_status_output(config: Config) -> str:
    payload = _status_payload(config)
    update = payload["update"]
    if not isinstance(update, dict):
        return "status unavailable"
    update = cast(dict[str, object], update)

    update_line = "Update: up to date"
    if update.get("available"):
        update_line = f"Update available ({update.get('current')} -> {update.get('latest')})"
    elif update.get("error"):
        update_line = f"Update check failed: {update.get('error')}"

    checked = update.get("checked_at")
    checked_txt = (
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(checked)))}"
        if isinstance(checked, (int, float))
        else "unknown"
    )

    return (
        f"Provider: {payload['provider']}\n"
        f"Model: {payload['model'] or 'default'}\n"
        f"Effort: {payload['effort']}\n"
        f"Version: {payload['version']}\n"
        f"{update_line}\n"
        f"Project: {payload['project']}\n"
        f"Checked: {checked_txt}\n"
        f"Home: {payload['home']}"
    )


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

    config = Config(provider=provider_name, model=args.model, effort=args.effort)
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


def status_command(args: argparse.Namespace) -> int:
    config = _config_store().load()
    if args.json:
        console.print(json.dumps(_status_payload(config), sort_keys=True, indent=2))
        return 0
    console.print(
        Panel.fit(_render_status_output(config), title="Milo status", border_style="cyan")
    )
    return 0


def update_command(args: argparse.Namespace) -> int:
    report = update_service.check_for_update(
        current=__version__,
        state_file=update_service.update_state_path(_home()),
        force=args.force,
        interval=args.interval,
    )
    if args.json:
        console.print(
            json.dumps(
                {
                    "current": report.current,
                    "latest": report.latest,
                    "available": report.available,
                    "checked_at": report.checked_at,
                    "error": report.error,
                    "interval": args.interval if args.interval is not None else None,
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 0 if report.error is None else 1
    if args.action == "check":
        if report.error:
            console.print(Text(f"Update check failed: {report.error}", style="yellow"))
            return 1
        if report.available and report.latest:
            console.print(f"Update available: {report.current} -> {report.latest}")
            return 0
        console.print(f"Milo is up to date ({report.current}).")
        return 0

    if report.error:
        console.print(Text(f"Update check failed: {report.error}", style="yellow"))
        return 1
    if not report.available:
        console.print(f"Milo is up to date ({report.current}).")
        return 0
    if not args.yes:
        accepted = console.input("Apply update now? [y/N]: ").strip().lower()
        if accepted not in {"y", "yes"}:
            console.print("Update cancelled.")
            return 0
    try:
        update_service.apply_update()
    except RuntimeError as exc:
        console.print(Text(f"Update failed: {exc}", style="red"))
        return 1
    console.print(f"Updated to {report.latest}. Restart Milo to use the new version.")
    return 0


def config_command(args: argparse.Namespace) -> int:
    config_path = _home() / "config.json"
    if args.config_action == "show":
        config = _config_store().load() if config_path.exists() else Config()
        console.print(json.dumps(asdict(config), sort_keys=True, indent=2))
        return 0

    if args.config_action == "set":
        config = _config_store().load() if config_path.exists() else Config()
        updates: dict[str, str] = {}
        if args.provider is not None:
            updates["provider"] = args.provider
        if args.model is not None:
            updates["model"] = args.model
        if args.effort is not None:
            updates["effort"] = args.effort

        if not updates:
            console.print(
                "[yellow]Usage: milo config set requires at least one of --provider, --model, --effort[/]"
            )
            return 2

        next_provider = updates.get("provider", config.provider)
        next_model = updates.get("model", config.model)
        next_effort = updates.get("effort", config.effort)

        next_config = replace(
            config,
            provider=next_provider,
            model=next_model,
            effort=next_effort,
        )
        _config_store().save(next_config)
        console.print(
            f"Updated config: provider={next_config.provider}, model={next_config.model or 'default'}, effort={next_config.effort}"
        )
        return 0

    raise ValueError(f"unknown config action: {args.config_action}")


def code_stats_command(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve()
    for candidate in repo_root.parents:
        if (candidate / "src" / "milo").exists():
            repo_root = candidate
            break
    else:
        console.print(f"Could not find source root from {__file__}")
        return 1

    all_python = sorted((repo_root / "src").rglob("*.py"))
    if args.include_tests:
        # Include tests at the same nesting level as src when explicitly requested.
        all_python.extend(sorted((repo_root / "tests").rglob("*.py")))
        # keep deterministic output when project layout has mixed paths
        all_python = sorted(set(all_python))
    else:
        # default: count only packaged runtime code.
        all_python = [path for path in all_python if "tests" not in path.parts]

    total_files = len(all_python)
    total_lines = 0
    for path in all_python:
        total_lines += len(path.read_text(encoding="utf-8", errors="ignore").splitlines())

    if args.json:
        console.print(
            json.dumps(
                {
                    "files": total_files,
                    "lines": total_lines,
                    "include_tests": args.include_tests,
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    console.print(f"python files: {total_files}")
    console.print(f"lines: {total_lines}")
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
                effort=args.effort if hasattr(args, "effort") else None,
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
    if getattr(args, "effort", None):
        config = replace(config, effort=args.effort)
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
            console.print(text, end="", markup=False)

        def show_delegation(decision: Any) -> None:
            console.print(Text(f"Sub-agents: {', '.join(decision.roles)}", style="dim"))

        result = Agent(
            config,
            _home() / "state.db",
            project=str(Path.cwd().resolve()),
            on_text=show_stream,
            on_delegation=show_delegation,
        ).run(task, resume=args.resume, delegate=False if args.no_delegate else None)
    except Exception as exc:
        console.print(Text("Milo could not complete the task:", style="red"), Text(str(exc)))
        return 1
    if streamed:
        console.print()
    else:
        console.print(result.text, markup=False)
    args.resume = result.session_id
    if getattr(args, "show_session", True):
        console.print(Text(f"Session: {result.session_id}", style="dim"))
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
            argparse.Namespace(
                provider=None, model=None, effort=None, skills=None, non_interactive=False
            )
        )
        if code:
            return code
    config = _config_store().load()
    console.print(_startup_panel(config, Path.cwd().resolve()))
    _check_update_availability(show_only_when_available=True)
    console.print("[bold]Welcome to Milo.[/] Type a task or /help for commands.\n")
    history_path = _home() / "history"
    history_path.touch(mode=0o600, exist_ok=True)
    os.chmod(history_path, 0o600)
    commands = [
        "/help",
        "/new",
        "/retry",
        "/clear",
        "/status",
        "/sessions",
        "/skills",
        "/memory",
        "/doctor",
        "/update",
        "/config",
        "/exit",
        "/quit",
    ]
    active_session: str | None = None
    last_prompt: str | None = None

    def toolbar() -> str:
        model = config.model or "default"
        effort = config.effort or "low"
        state = "active session" if active_session else "new session"
        return (
            f" {config.provider} · {model} · {effort} │ {state} │ Enter: submit · Ctrl+C: cancel "
        )

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(commands, sentence=True),
        bottom_toolbar=toolbar,
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
            console.print(
                Panel.fit(
                    "[bold]Session[/]  /new /retry /clear /status /sessions /config\n"
                    "[bold]Knowledge[/] /skills /memory\n"
                    "[bold]System[/]   /doctor /update /help /exit\n\n"
                    "Complex tasks delegate automatically; provider sessions continue natively.",
                    title="Milo commands",
                    border_style="cyan",
                )
            )
            continue
        if prompt == "/new":
            active_session = None
            last_prompt = None
            console.print("[cyan]Started a new Milo session.[/]")
            continue
        if prompt == "/clear":
            active_session = None
            last_prompt = None
            console.clear()
            console.print(_startup_panel(config, Path.cwd().resolve()))
            continue
        if prompt == "/status":
            console.print(
                Panel.fit(
                    _render_status_output(config)
                    + f"\nSession: {'active' if active_session else 'new'}",
                    title="Milo status",
                    border_style="cyan",
                )
            )
            continue
        if prompt == "/sessions":
            sessions_command(argparse.Namespace(sessions_action="list", id=None, query=None))
            continue
        if prompt == "/skills":
            skills_command(argparse.Namespace(skill_action="list", name=None))
            continue
        if prompt == "/memory":
            memory_command(
                argparse.Namespace(memory_action="list", content=None, query=None, id=None)
            )
            continue
        if prompt == "/doctor":
            doctor(argparse.Namespace())
            continue
        if prompt.startswith("/config"):
            parts = prompt.split()
            if len(parts) == 1 or parts[1] == "show":
                config_command(argparse.Namespace(config_action="show"))
                continue
            if parts[1] != "set":
                console.print(
                    "[yellow]usage: /config show | /config set [--provider codex|claude|gemini] [--model <name>] [--effort low|medium|high][/ ]"
                )
                continue
            parsed = argparse.Namespace(provider=None, model=None, effort=None)
            index = 2
            valid = True
            while index < len(parts):
                key = parts[index]
                if key not in {"--provider", "--model", "--effort"}:
                    valid = False
                    break
                if index + 1 >= len(parts):
                    valid = False
                    break
                value = parts[index + 1]
                if key == "--provider":
                    if value not in {"codex", "claude", "gemini"}:
                        valid = False
                        break
                    parsed.provider = value
                elif key == "--model":
                    parsed.model = value
                elif key == "--effort":
                    if value not in {"low", "medium", "high"}:
                        valid = False
                        break
                    parsed.effort = value
                index += 2
            if not valid:
                console.print(
                    "[yellow]usage: /config set [--provider codex|claude|gemini] [--model <name>] [--effort low|medium|high][/ ]"
                )
                continue
            if parsed.provider is None and parsed.model is None and parsed.effort is None:
                console.print("[yellow]use --provider, --model, or --effort with /config set[/]")
                continue
            config_command(
                argparse.Namespace(
                    config_action="set",
                    provider=parsed.provider,
                    model=parsed.model,
                    effort=parsed.effort,
                )
            )
            continue
        if prompt.startswith("/update"):
            parts = prompt.split()
            action = "check"
            if len(parts) > 1:
                action = parts[1]
                if action not in {"check", "apply"}:
                    console.print(
                        "[yellow]usage: /update [check|apply] [--yes|-y] [--interval N][/]"
                    )
                    continue
            options = {"yes": False, "force": False}
            interval: float | None = None
            if "--yes" in parts or "-y" in parts:
                options["yes"] = True
            if "--force" in parts:
                options["force"] = True
            if "--interval" in parts:
                idx = parts.index("--interval")
                if idx + 1 >= len(parts):
                    console.print("[yellow]usage: /update ... --interval <seconds>[/]")
                    continue
                try:
                    interval = float(parts[idx + 1])
                except ValueError:
                    console.print("[yellow]--interval requires a numeric seconds value[/]")
                    continue
            args = argparse.Namespace(
                action=action,
                yes=options["yes"],
                force=options["force"],
                json=False,
                interval=interval,
            )
            update_command(args)
            continue
        if prompt == "/retry":
            if not last_prompt:
                console.print("[yellow]Nothing to retry in this session.[/]")
                continue
            prompt = last_prompt
        if prompt:
            chat_args = argparse.Namespace(
                prompt=[prompt],
                provider=None,
                model=None,
                resume=active_session,
                no_delegate=False,
                show_session=False,
            )
            if chat(chat_args) == 0:
                active_session = chat_args.resume
                last_prompt = prompt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="milo", description="Provider-native AI agent CLI")
    parser.add_argument("--version", action="version", version=f"Milo {__version__}")
    sub = parser.add_subparsers(dest="command")

    setup_parser = sub.add_parser("setup", help="configure a provider and skills")
    setup_parser.add_argument("--provider", choices=PROVIDERS)
    setup_parser.add_argument("--model")
    setup_parser.add_argument(
        "--effort",
        choices=("low", "medium", "high"),
        default="low",
    )
    setup_parser.add_argument("--skills", choices=("recommended", "all", "none"))
    setup_parser.add_argument("--non-interactive", action="store_true")

    sub.add_parser("doctor", help="diagnose installation")

    update_parser = sub.add_parser("update", help="check or apply updates")
    update_parser.add_argument("action", nargs="?", choices=("check", "apply"), default="check")
    update_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="apply update without confirmation",
    )
    update_parser.add_argument("--json", action="store_true", help="print machine-readable status")
    update_parser.add_argument(
        "--force",
        action="store_true",
        help="force refresh of update metadata before acting",
    )
    update_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="override update check interval in seconds",
    )

    config_parser = sub.add_parser("config", help="show and update CLI defaults")
    config_sub = config_parser.add_subparsers(dest="config_action", required=True)
    config_sub.add_parser("show")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("--provider", choices=PROVIDERS)
    config_set.add_argument("--model")
    config_set.add_argument("--effort", choices=("low", "medium", "high"))

    status_parser = sub.add_parser("status", help="show current CLI status")
    status_parser.add_argument("--json", action="store_true", help="output JSON payload")

    loc_parser = sub.add_parser("loc", help="count source lines of code")
    loc_parser.add_argument("--json", action="store_true", help="output JSON payload")
    loc_parser.add_argument("--include-tests", action="store_true", help="include tests directory")

    chat_parser = sub.add_parser("chat", help="run a task")
    chat_parser.add_argument("prompt", nargs="*")
    chat_parser.add_argument("--provider", choices=PROVIDERS)
    chat_parser.add_argument("--model")
    chat_parser.add_argument(
        "--effort",
        choices=("low", "medium", "high"),
    )
    chat_parser.add_argument("--resume")
    chat_parser.add_argument("--no-delegate", action="store_true")
    chat_parser.add_argument("--show-session", action="store_true")

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
        "update": update_command,
        "status": status_command,
        "loc": code_stats_command,
        "config": config_command,
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
