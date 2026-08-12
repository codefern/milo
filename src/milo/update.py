from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import time as _time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__

GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/codefern/milo/releases/latest"
DEFAULT_INSTALL_SOURCE = "git+https://github.com/codefern/milo.git"
UPDATE_STATE_FILE = "update.json"
_DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_DEFAULT_UPDATE_TIMEOUT = 5


_VERSION_RE = re.compile(r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)")


@dataclass(frozen=True)
class UpdateState:
    last_check: float
    latest: str | None
    error: str | None = None


@dataclass(frozen=True)
class UpdateReport:
    current: str
    latest: str | None
    available: bool
    checked_at: float
    error: str | None = None


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _is_newer_version(candidate: str, current: str) -> bool:
    parsed_candidate = _parse_version(candidate)
    parsed_current = _parse_version(current)
    if parsed_candidate is None or parsed_current is None:
        return False
    return parsed_candidate > parsed_current


def update_state_path(home: Path) -> Path:
    return home / UPDATE_STATE_FILE


def _parse_interval(raw_interval: str | None) -> float:
    if raw_interval is None:
        return _DEFAULT_CHECK_INTERVAL_SECONDS
    text = raw_interval.strip()
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ValueError(
            "MILO_UPDATE_CHECK_INTERVAL_SECONDS must be a non-negative number"
        ) from exc
    if parsed < 0:
        raise ValueError("MILO_UPDATE_CHECK_INTERVAL_SECONDS must be a non-negative number")
    return parsed


def resolve_update_interval(cli_interval: float | None = None) -> float:
    if cli_interval is not None:
        if cli_interval < 0:
            raise ValueError("Update interval must be a non-negative number")
        return cli_interval
    return _parse_interval(os.getenv("MILO_UPDATE_CHECK_INTERVAL_SECONDS"))


def load_update_state(path: Path) -> UpdateState:
    if not path.exists():
        return UpdateState(0.0, None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return UpdateState(0.0, None)
        return UpdateState(
            float(data.get("last_check", 0.0)),
            data.get("latest"),
            data.get("error"),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return UpdateState(0.0, None)


def save_update_state(path: Path, state: UpdateState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "last_check": state.last_check,
        "latest": state.latest,
        "error": state.error,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _needs_network_refresh(state: UpdateState, *, now: float, interval: float) -> bool:
    if state.latest is None:
        return True
    return (now - state.last_check) >= interval


def fetch_latest_version(
    *, opener: Callable[..., Any] = urlopen, timeout: int = _DEFAULT_UPDATE_TIMEOUT
) -> str | None:
    request = Request(
        GITHUB_LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "milo-updater"},
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.load(response)
    except (URLError, OSError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        return None
    parsed = tag[1:] if tag.startswith("v") else tag
    return parsed if _parse_version(parsed) is not None else None


def check_for_update(
    *,
    current: str = __version__,
    state_file: Path | None = None,
    force: bool = False,
    interval: float | None = None,
    now: float | None = None,
    opener: Callable[..., Any] = urlopen,
    timeout: int = _DEFAULT_UPDATE_TIMEOUT,
) -> UpdateReport:
    current_time = _time() if now is None else now
    resolved_interval = resolve_update_interval(interval)
    state = UpdateState(0.0, None) if state_file is None else load_update_state(state_file)

    latest = state.latest
    error: str | None = state.error
    if (
        force
        or state_file is None
        or _needs_network_refresh(state, now=current_time, interval=resolved_interval)
    ):
        fetched = fetch_latest_version(opener=opener, timeout=timeout)
        if fetched is None:
            latest = state.latest
            error = "failed to read latest release"
        else:
            latest = fetched
            error = None
        state = UpdateState(current_time, latest, error)
        if state_file is not None:
            save_update_state(state_file, state)

    available = bool(latest and _is_newer_version(latest, current))

    return UpdateReport(
        current=current,
        latest=latest,
        available=available,
        checked_at=current_time,
        error=error,
    )


def default_install_command() -> list[str]:
    uv_path = shutil.which("uv")
    if uv_path:
        return [uv_path, "tool", "install", "--force", DEFAULT_INSTALL_SOURCE]
    pipx_path = shutil.which("pipx")
    if pipx_path:
        return [pipx_path, "install", "--force", DEFAULT_INSTALL_SOURCE]
    raise RuntimeError("No supported update tool found (uv or pipx required)")


def apply_update(
    *,
    command: list[str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    argv = command or default_install_command()
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(result, subprocess.CompletedProcess):
        result = subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "milo update failed").strip())
    return result
