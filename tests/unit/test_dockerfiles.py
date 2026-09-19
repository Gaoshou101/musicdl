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


def test_panel_image_pins_node_builds_standalone_and_runs_non_root():
    text = (ROOT / "docker/web/Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22.23.2-slim" in text
    assert text.count("FROM node:22.23.2-slim") == 3, "deps, builder, and runner are the same pinned base"
    assert "npm ci" in text
    assert "npm run build" in text
    assert "USER 10001:10001" in text
    assert "groupadd --gid 10001" in text
    assert text.index("groupadd") < text.index("useradd")
    assert "useradd --uid 10001" in text
    assert "--shell /usr/sbin/nologin" in text
    assert ".[dev]" not in text


def test_panel_image_ships_the_standalone_server_and_its_static_chunks():
    text = (ROOT / "docker/web/Dockerfile").read_text(encoding="utf-8")
    runner = text[text.rindex("FROM "):]
    # `standalone` carries the server and its slice of node_modules, but never
    # the static chunks: those have to be copied beside it or every page loads
    # without its JavaScript.
    assert "COPY --from=builder --chown=10001:10001 /build/.next/standalone ./" in runner
    assert "COPY --from=builder --chown=10001:10001 /build/.next/static ./.next/static" in runner
    assert 'CMD ["node", "server.js"]' in runner
    assert "/app/.next/cache" in runner, "the runtime needs a writable cache beside a read-only root"


def test_panel_image_takes_the_app_origin_at_build_time():
    """Next resolves its rewrites during `next build`, so the app address is an ARG.

    A runtime ENV would be read by nothing: the rewrite is already compiled into
    the server by the time the container starts.
    """
    text = (ROOT / "docker/web/Dockerfile").read_text(encoding="utf-8")
    assert "ARG MUSICDL_API_ORIGIN=http://musicdl:8000" in text
    builder = text[text.index("FROM node:22.23.2-slim AS builder"):text.index("FROM node:22.23.2-slim AS runner")]
    assert "ENV MUSICDL_API_ORIGIN=${MUSICDL_API_ORIGIN}" in builder
    assert "npm run build" in builder
    runner = text[text.rindex("FROM "):]
    assert "MUSICDL_API_ORIGIN" not in runner


def test_dockerignore_keeps_the_panel_build_output_out_of_the_context():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("web/node_modules", "web/.next", "web/tsconfig.tsbuildinfo"):
        assert pattern in text
