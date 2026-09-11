from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dockerfiles_are_pinned_non_root_and_production_only():
    for path in (ROOT / "docker/main/Dockerfile", ROOT / "docker/plugin/Dockerfile"):
        text = path.read_text(encoding="utf-8")
        assert "FROM python:3.12.14-slim" in text
        assert "USER 10001:10001" in text
        assert "--no-dev" in text or "--no-cache-dir" in text
        assert "groupadd" in text
        assert text.index("groupadd") < text.index("useradd")
        assert ".[dev]" not in text
        assert "EXPOSE" not in text


def test_dockerignore_excludes_secrets_sessions_media_and_caches():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".git", ".venv", ".env", "telegram-sessions", "data/music", "__pycache__", ".pytest_cache"):
        assert pattern in text
