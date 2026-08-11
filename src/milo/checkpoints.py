from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import suppress
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
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("checkpoint path is outside workspace")
        lexical = self.workspace / relative_path
        cursor = lexical
        while cursor != self.workspace:
            if cursor.is_symlink():
                raise ValueError("checkpoint paths may not contain symlinks")
            cursor = cursor.parent
        path = lexical.resolve(strict=False)
        if path != self.workspace and self.workspace not in path.parents:
            raise ValueError("checkpoint path is outside workspace")
        return path

    def _open_parent(self, relative: str, *, create: bool) -> tuple[int, str]:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
            raise ValueError("checkpoint path is outside workspace")
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise OSError("secure checkpoint operations require no-follow directory descriptors")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.workspace, flags)
        try:
            for part in relative_path.parts[:-1]:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor, relative_path.parts[-1]
        except Exception:
            os.close(descriptor)
            raise

    def _read_workspace(self, relative: str) -> bytes:
        parent, name = self._open_parent(relative, create=False)
        descriptor = -1
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("checkpoint source must be a regular, unlinked file")
            chunks = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)

    def _write_workspace(self, relative: str, content: bytes) -> None:
        parent, name = self._open_parent(relative, create=True)
        temporary = f".milo-restore-{uuid.uuid4().hex}"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)
            os.close(parent)

    def create(self, label: str, files: list[str]) -> Checkpoint:
        identifier = uuid.uuid4().hex
        target = self.root / identifier
        target.mkdir(mode=0o700)
        saved: list[str] = []
        hashes: dict[str, str] = {}
        for relative in dict.fromkeys(files):
            self._path(relative)
            try:
                content = self._read_workspace(relative)
            except FileNotFoundError:
                continue
            else:
                destination = target / "files" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                os.chmod(destination, 0o400)
                saved.append(relative)
                hashes[relative] = hashlib.sha256(content).hexdigest()
        checkpoint = Checkpoint(
            identifier, label, datetime.now(UTC).isoformat(), tuple(saved), hashes
        )
        manifest = target / "manifest.json"
        manifest.write_text(json.dumps(asdict(checkpoint), indent=2) + "\n")
        os.chmod(manifest, 0o400)
        for directory in sorted(target.rglob("*"), reverse=True):
            if directory.is_dir():
                os.chmod(directory, 0o500)
        os.chmod(target, 0o500)
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
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != checkpoint.hashes[relative]:
                raise ValueError("checkpoint integrity failure")
            self._write_workspace(relative, content)
        return checkpoint
