"""Validated skill catalogs and safe structured installation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import Any


class SkillError(ValueError):
    """A skill package or catalog violates Milo's validation policy."""


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_TRUSTED_SOURCES = {"builtin", "local"}


def _version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if not match:
        raise SkillError(f"invalid version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _compatible(current: str, requirement: str) -> bool:
    value = _version(current)
    if not requirement:
        return True
    for clause in requirement.split(","):
        clause = clause.strip()
        operator = next((op for op in (">=", "<=", "==", ">", "<") if clause.startswith(op)), None)
        if operator is None:
            raise SkillError(f"invalid compatibility requirement: {requirement}")
        target = _version(clause[len(operator) :])
        checks = {
            ">=": value >= target,
            "<=": value <= target,
            "==": value == target,
            ">": value > target,
            "<": value < target,
        }
        if not checks[operator]:
            return False
    return True


def _safe_relative(name: str) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise SkillError(f"unsafe skill file path: {name}")
    return Path(*pure.parts)


@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    source: str
    requires_milo: str
    files: dict[str, str]
    install_argv: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, milo_version: str) -> SkillManifest:
        required = {"name", "version", "source", "requires_milo", "files"}
        missing = required - data.keys()
        if missing:
            raise SkillError(f"missing manifest field: {sorted(missing)[0]}")
        name = data["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SkillError("invalid skill name")
        _version(data["version"])
        if data["source"] not in _TRUSTED_SOURCES:
            raise SkillError("untrusted skill source")
        if not isinstance(data["files"], dict) or not data["files"]:
            raise SkillError("manifest files must be a non-empty object")
        files: dict[str, str] = {}
        for file_name, digest in data["files"].items():
            _safe_relative(file_name)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise SkillError(f"invalid hash for {file_name}")
            files[file_name] = digest.lower()
        requirement = data["requires_milo"]
        if not isinstance(requirement, str) or not _compatible(milo_version, requirement):
            raise SkillError("skill is incompatible with this Milo version")
        argv = data.get("install_argv", [])
        if not isinstance(argv, list) or not all(isinstance(arg, str) and arg for arg in argv):
            raise SkillError("install_argv must be an argv string list")
        return cls(name, data["version"], data["source"], requirement, files, tuple(argv))

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "requires_milo": self.requires_milo,
            "files": self.files,
        }
        if self.install_argv:
            result["install_argv"] = list(self.install_argv)
        return result


def load_manifest(path: str | PathLike[str], *, milo_version: str) -> SkillManifest:
    manifest_path = Path(path)
    if manifest_path.is_symlink():
        raise SkillError("manifest may not be a symlink")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillError(f"cannot load skill manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillError("skill manifest must be an object")
    return SkillManifest.from_dict(data, milo_version=milo_version)


def validate_skill(
    path: str | PathLike[str], *, milo_version: str, require_matching_directory: bool = True
) -> SkillManifest:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise SkillError("skill root must be a real directory, not a symlink")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise SkillError(f"skill may not contain symlink: {item.name}")
    manifest = load_manifest(root / "skill.json", milo_version=milo_version)
    if require_matching_directory and root.name != manifest.name:
        raise SkillError("skill directory must match manifest name")
    for relative_name, expected in manifest.files.items():
        file_path = root / _safe_relative(relative_name)
        if not file_path.is_file():
            raise SkillError(f"missing skill file: {relative_name}")
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual != expected:
            raise SkillError(f"hash mismatch: {relative_name}")
    return manifest


def load_catalog(path: str | PathLike[str], *, milo_version: str) -> list[SkillManifest]:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise SkillError("catalog does not exist or is a symlink")
    manifests = [
        validate_skill(child, milo_version=milo_version)
        for child in sorted(root.iterdir(), key=lambda item: item.name)
        if child.is_dir() and not child.is_symlink()
    ]
    names = [manifest.name for manifest in manifests]
    if len(names) != len(set(names)):
        raise SkillError("duplicate skill name")
    return manifests


class SkillInstaller:
    def __init__(self, destination: str | PathLike[str], *, milo_version: str) -> None:
        self.destination = Path(destination)
        self.milo_version = milo_version

    def _target(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise SkillError("invalid skill name")
        return self.destination / name

    def _stage(self, source: Path, manifest: SkillManifest) -> Path:
        self.destination.mkdir(parents=True, exist_ok=True)
        staging = self.destination / f".{manifest.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            for name in manifest.files:
                relative = _safe_relative(name)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, target, follow_symlinks=False)
            (staging / "skill.json").write_text(
                json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            validate_skill(
                staging, milo_version=self.milo_version, require_matching_directory=False
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return staging

    def install(
        self,
        source: str | PathLike[str],
        *,
        approve_install_commands: bool = False,
    ) -> Path:
        source_path = Path(source)
        manifest = validate_skill(source_path, milo_version=self.milo_version)
        if manifest.install_argv and not approve_install_commands:
            raise SkillError("install commands require explicit approval")
        target = self._target(manifest.name)
        if target.exists() or target.is_symlink():
            raise SkillError("skill is already installed")
        staging = self._stage(source_path, manifest)
        staging.replace(target)
        return target

    def update(
        self,
        source: str | PathLike[str],
        *,
        approve_install_commands: bool = False,
    ) -> Path:
        source_path = Path(source)
        manifest = validate_skill(source_path, milo_version=self.milo_version)
        if manifest.install_argv and not approve_install_commands:
            raise SkillError("install commands require explicit approval")
        target = self._target(manifest.name)
        if not target.is_dir() or target.is_symlink():
            raise SkillError("skill is not installed")
        staging = self._stage(source_path, manifest)
        backup = self.destination / f".{manifest.name}.backup-{uuid.uuid4().hex}"
        target.replace(backup)
        try:
            staging.replace(target)
        except Exception:
            backup.replace(target)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(backup)
        return target

    def remove(self, name: str) -> bool:
        target = self._target(name)
        if target.is_symlink():
            raise SkillError("refusing to remove symlink")
        if not target.exists():
            return False
        if not target.is_dir():
            raise SkillError("installed skill is not a directory")
        load_manifest(target / "skill.json", milo_version=self.milo_version)
        shutil.rmtree(target)
        return True

    def list_installed(self) -> list[SkillManifest]:
        if not self.destination.exists():
            return []
        return load_catalog(self.destination, milo_version=self.milo_version)
