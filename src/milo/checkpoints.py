from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Checkpoint:
    id: str
    label: str
    created_at: str
    files: tuple[str, ...]
    hashes: dict[str, str]


class CheckpointStore:
    def __init__(self, state: str | Path, workspace: str | Path) -> None:
        self.state = Path(state).resolve()
        self.workspace = Path(workspace).resolve(strict=True)
        self.root = self.state / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, relative: str) -> Path:
        path = (self.workspace / relative).resolve(strict=False)
        if path != self.workspace and self.workspace not in path.parents:
            raise ValueError("checkpoint path is outside workspace")
        if path.is_symlink():
            raise ValueError("checkpoint paths may not be symlinks")
        return path

    def create(self, label: str, files: list[str]) -> Checkpoint:
        identifier = uuid.uuid4().hex
        target = self.root / identifier
        target.mkdir(mode=0o700)
        saved: list[str] = []
        hashes: dict[str, str] = {}
        for relative in dict.fromkeys(files):
            source = self._path(relative)
            if source.is_file():
                destination = target / "files" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                saved.append(relative)
                hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
        checkpoint = Checkpoint(
            identifier, label, datetime.now(UTC).isoformat(), tuple(saved), hashes
        )
        (target / "manifest.json").write_text(json.dumps(asdict(checkpoint), indent=2) + "\n")
        return checkpoint

    def get(self, identifier: str) -> Checkpoint:
        if not identifier.isalnum():
            raise ValueError("invalid checkpoint id")
        data = json.loads((self.root / identifier / "manifest.json").read_text())
        return Checkpoint(
            data["id"],
            data["label"],
            data["created_at"],
            tuple(data["files"]),
            dict(data["hashes"]),
        )

    def list(self) -> list[Checkpoint]:
        result = []
        for manifest in sorted(self.root.glob("*/manifest.json"), reverse=True):
            try:
                result.append(self.get(manifest.parent.name))
            except (OSError, ValueError, json.JSONDecodeError, KeyError):
                continue
        return result

    def restore(self, identifier: str) -> Checkpoint:
        checkpoint = self.get(identifier)
        source_root = self.root / identifier / "files"
        for relative in checkpoint.files:
            source = source_root / relative
            destination = self._path(relative)
            if hashlib.sha256(source.read_bytes()).hexdigest() != checkpoint.hashes[relative]:
                raise ValueError("checkpoint integrity failure")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return checkpoint
