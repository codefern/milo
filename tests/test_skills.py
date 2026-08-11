import hashlib
import json
from pathlib import Path

import pytest

from milo.skills import SkillError, SkillInstaller, SkillManifest, load_catalog


def make_skill(root: Path, *, name: str = "example", content: str = "hello") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    (skill / "skill.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "source": "local",
                "requires_milo": ">=0.1.0,<1.0.0",
                "files": {"SKILL.md": digest},
            }
        )
    )
    return skill


def test_catalog_loads_valid_trusted_skill_and_installs_structured_copy(tmp_path: Path) -> None:
    source = make_skill(tmp_path / "catalog")
    manifest = load_catalog(source.parent, milo_version="0.1.0")[0]
    installer = SkillInstaller(tmp_path / "installed", milo_version="0.1.0")

    installed = installer.install(source)

    assert manifest == SkillManifest(
        name="example",
        version="1.0.0",
        source="local",
        requires_milo=">=0.1.0,<1.0.0",
        files={"SKILL.md": hashlib.sha256(b"hello").hexdigest()},
        install_argv=(),
    )
    assert (installed / "SKILL.md").read_text() == "hello"
    assert installer.list_installed() == [manifest]


def test_install_rejects_hash_mismatch_symlink_and_traversal(tmp_path: Path) -> None:
    source = make_skill(tmp_path / "catalog")
    installer = SkillInstaller(tmp_path / "installed", milo_version="0.1.0")
    (source / "SKILL.md").write_text("tampered")
    with pytest.raises(SkillError, match="hash"):
        installer.install(source)

    source = make_skill(tmp_path / "other", name="linked")
    (source / "link").symlink_to(source / "SKILL.md")
    with pytest.raises(SkillError, match="symlink"):
        installer.install(source)

    source = make_skill(tmp_path / "third", name="traversal")
    data = json.loads((source / "skill.json").read_text())
    data["files"] = {"../outside": "0" * 64}
    (source / "skill.json").write_text(json.dumps(data))
    with pytest.raises(SkillError, match="path"):
        installer.install(source)


def test_commands_need_approval_and_update_remove_are_safe(tmp_path: Path) -> None:
    source = make_skill(tmp_path / "catalog")
    data = json.loads((source / "skill.json").read_text())
    data["install_argv"] = ["python", "setup.py"]
    (source / "skill.json").write_text(json.dumps(data))
    installer = SkillInstaller(tmp_path / "installed", milo_version="0.1.0")

    with pytest.raises(SkillError, match="approval"):
        installer.install(source)
    installer.install(source, approve_install_commands=True)

    (source / "SKILL.md").write_text("version two")
    data["version"] = "2.0.0"
    data["files"]["SKILL.md"] = hashlib.sha256(b"version two").hexdigest()
    (source / "skill.json").write_text(json.dumps(data))
    installer.update(source, approve_install_commands=True)
    assert (tmp_path / "installed/example/SKILL.md").read_text() == "version two"

    assert installer.remove("example") is True
    assert installer.remove("example") is False
    with pytest.raises(SkillError):
        installer.remove("../outside")
