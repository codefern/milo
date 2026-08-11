from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROVIDERS = ("codex", "claude", "gemini")


@dataclass(frozen=True)
class Config:
    provider: str = "codex"
    model: str | None = None
    max_agents: int = 3
    context_budget: int = 24_000

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"provider must be one of: {', '.join(PROVIDERS)}")
        if not 1 <= self.max_agents <= 8:
            raise ValueError("max_agents must be between 1 and 8")

        if self.context_budget < 1000:
            raise ValueError("context_budget must be at least 1000")


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Config:
        if not self.path.exists():
            return Config()
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration must be an object")
            data.pop("permissions", None)  # migrate pre-1.0 release-candidate state
            config = Config(**data)
            config.validate()
            return config
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"cannot load Milo configuration: {exc}") from exc

    def save(self, config: Config) -> None:
        config.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(config), stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)
