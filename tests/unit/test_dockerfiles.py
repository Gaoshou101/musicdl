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
        assert "--root-user-action=ignore" in text
        assert "groupadd --system" not in text
        assert "useradd --system" not in text
        assert "--shell /usr/sbin/nologin" in text


def test_plugin_image_pins_deno_and_libseccomp_with_verified_multiarch_download():
    text = (ROOT / "docker/plugin/Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12.14-slim-bookworm" in text
    assert "DENO_VERSION=2.9.6" in text
    assert "deno-x86_64-unknown-linux-gnu.zip" in text
    assert "deno-aarch64-unknown-linux-gnu.zip" in text
    assert "394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2" in text
    assert "9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e" in text
    assert "sha256sum -c" in text
    download = text[text.index("WORKDIR /tmp/deno"):]
    assert download.index("sha256sum -c") < download.index("unzip")
    assert "libseccomp2=2.5.4-1+deb12u1" in text
    assert "deno --version" in text


def test_plugin_image_final_stage_has_only_runtime_packages():
    text = (ROOT / "docker/plugin/Dockerfile").read_text(encoding="utf-8")
    final = text[text.rfind("FROM "):]
    assert "apt-get install" in final
    assert "ca-certificates" in final
    assert "libseccomp2=2.5.4-1+deb12u1" in final
    assert "unzip" not in final
    assert "curl" not in final
    assert "pip install --no-cache-dir --timeout 120 --root-user-action=ignore ." in final


def test_dockerignore_excludes_secrets_sessions_media_and_caches():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".git", ".venv", ".env", "telegram-sessions", "data/music", "__pycache__", ".pytest_cache"):
        assert pattern in text


def test_plugin_runner_packages_deno_host_asset():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'musicdl_plugin_runner = ["*.js"]' in text
