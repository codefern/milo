from __future__ import annotations

from pathlib import Path

from milo.context import ContextSelector, estimate_tokens


def test_context_prioritizes_requested_paths_and_keyword_matches(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "requested.py").write_text("requested content\n", encoding="utf-8")
    (tmp_path / "src" / "match.py").write_text("orchid implementation\n", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("nothing useful\n", encoding="utf-8")

    result = ContextSelector(tmp_path).select(
        keywords=["orchid"], paths=["src/requested.py"], token_budget=100
    )

    assert [item.path.as_posix() for item in result.items] == [
        "src/requested.py",
        "src/match.py",
    ]
    assert result.used_tokens <= result.token_budget


def test_context_never_reads_git_or_binary_files(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("orchid", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"orchid\x00payload")
    (tmp_path / "visible.txt").write_text("orchid", encoding="utf-8")

    result = ContextSelector(tmp_path).select(keywords=["orchid"], token_budget=100)

    assert [item.path.as_posix() for item in result.items] == ["visible.txt"]
    assert {item.path.as_posix() for item in result.ignored} == {"binary.dat"}


def test_context_uses_progressive_disclosure_within_hard_budget(tmp_path: Path) -> None:
    content = "\n".join(f"line {number} orchid details" for number in range(100))
    (tmp_path / "large.txt").write_text(content, encoding="utf-8")
    budget = 30

    result = ContextSelector(tmp_path).select(keywords=["orchid"], token_budget=budget)

    assert result.used_tokens <= budget
    assert len(result.items) == 1
    assert result.items[0].truncated
    assert "orchid" in result.items[0].content
    assert estimate_tokens(result.items[0].content) <= budget


def test_context_rejects_paths_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret orchid", encoding="utf-8")

    result = ContextSelector(tmp_path).select(paths=["../outside.txt"], token_budget=100)

    assert result.items == []
