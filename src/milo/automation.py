from __future__ import annotations

import builtins
import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Automation:
    id: str
    name: str
    schedule: str
    prompt: str
    enabled: bool = False


class AutomationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def list(self) -> builtins.list[Automation]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        return [Automation(**item) for item in data]

    def _save(self, jobs: builtins.list[Automation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(json.dumps([asdict(job) for job in jobs], indent=2) + "\n")
        os.chmod(self.path, 0o600)

    def create(self, name: str, schedule: str, prompt: str) -> Automation:
        if not name.strip() or not schedule.strip() or not prompt.strip():
            raise ValueError("name, schedule, and prompt are required")
        job = Automation(uuid.uuid4().hex, name, schedule, prompt, False)
        jobs = self.list() + [job]
        self._save(jobs)
        return job

    def set_enabled(self, identifier: str, enabled: bool) -> Automation:
        jobs = self.list()
        for index, job in enumerate(jobs):
            if job.id == identifier:
                jobs[index] = replace(job, enabled=enabled)
                self._save(jobs)
                return jobs[index]
        raise KeyError(identifier)

    def remove(self, identifier: str) -> bool:
        jobs = self.list()
        remaining = [job for job in jobs if job.id != identifier]
        if len(remaining) == len(jobs):
            return False
        self._save(remaining)
        return True
