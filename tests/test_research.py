from pathlib import Path

import pytest

from milo.research import ResearchError, Source, synthesize_sources, validate_public_url


def test_research_rejects_local_network_and_tracks_sources() -> None:
    with pytest.raises(ResearchError):
        validate_public_url("http://127.0.0.1/private")
    with pytest.raises(ResearchError):
        validate_public_url("https://localhost/private")
    assert validate_public_url("https://example.com/docs") == "https://example.com/docs"
    report = synthesize_sources(
        "topic",
        [Source("https://a.example", "A", "alpha"), Source("https://b.example", "B", "beta")],
    )
    assert "https://a.example" in report
    assert "https://b.example" in report


def test_context_walk_excludes_common_caches(tmp_path: Path) -> None:
    from milo.context import ContextSelector

    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("important handler")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/secret.py").write_text("important but irrelevant")
    (tmp_path / ".env").write_text("IMPORTANT_TOKEN=do-not-load")
    result = ContextSelector(tmp_path).select(keywords=["important"], token_budget=100)
    assert [item.path.as_posix() for item in result.items] == ["src/app.py"]
