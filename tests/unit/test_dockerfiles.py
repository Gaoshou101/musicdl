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
    runner_wheel = "/tmp/musicdl_plugin_runner-1.0.0-py3-none-any.whl"
    assert f"pip install --no-cache-dir --timeout 120 --root-user-action=ignore {runner_wheel}" in final
    assert f"rm -f {runner_wheel}" in final
    assert "/tmp/musicdl-plugin-runner.whl" not in final
    assert "COPY src ./src" not in final
    assert "COPY pyproject.toml README.md ./" not in final


def test_main_image_builds_and_installs_a_versioned_wheel_without_source_tree():
    text = (ROOT / "docker/main/Dockerfile").read_text(encoding="utf-8")
    package_stage = text.index("FROM python:3.12.14-slim AS app-package")
    runtime_stage = text.rindex("FROM python:3.12.14-slim")
    builder = text[package_stage:runtime_stage]
    runtime = text[runtime_stage:]
    wheel = "musicdl-1.0.0-py3-none-any.whl"

    assert "COPY pyproject.toml README.md ./" in builder
    assert "COPY src/musicdl ./src/musicdl" in builder
    assert "COPY src/musicdl_plugin_runner ./src/musicdl_plugin_runner" in builder
    assert "pip wheel --no-cache-dir --no-deps --wheel-dir /wheels ." in builder
    assert f"COPY --from=app-package /wheels/{wheel} /tmp/{wheel}" in runtime
    assert f"pip install --no-cache-dir --root-user-action=ignore /tmp/{wheel}" in runtime
    assert f"rm -f /tmp/{wheel}" in runtime
    assert "COPY src ./src" not in runtime
    assert "COPY pyproject.toml README.md ./" not in runtime
    assert "npm" not in runtime and "node_modules" not in runtime


def test_dockerignore_excludes_secrets_sessions_media_and_caches():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".git", ".venv", ".env", "telegram-sessions", "data/music", "__pycache__", ".pytest_cache"):
        assert pattern in text


def test_plugin_runner_packages_deno_host_asset():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'musicdl_plugin_runner = ["*.js"]' in text


def test_the_console_is_built_into_the_app_image_and_not_into_one_of_its_own():
    """The console is static files, so the runtime stage carries files, not Node.

    A separate panel image would put a Node runtime back in production for a
    build step, publish a second port, and need a forwarding hop. Asserting that
    the export is copied in -- and that no panel Dockerfile exists to build
    instead -- is what keeps the deployment at two containers.
    """
    text = (ROOT / "docker/main/Dockerfile").read_text(encoding="utf-8")
    assert not (ROOT / "docker/web/Dockerfile").exists()
    panel = text[text.index("FROM node:22.23.2-slim AS panel"):text.index("FROM python:3.12.14-slim")]
    assert "npm ci" in panel and "npm run build" in panel
    runtime = text[text.rindex("FROM python:3.12.14-slim"):]
    assert "COPY --from=panel --chown=10001:10001 /build/out /app/panel" in runtime
    assert "ENV MUSICDL_ADMIN__PANEL_ROOT=/app/panel" in runtime
    # Nothing from the Node toolchain survives into the stage that runs.
    assert "npm" not in runtime and "node_modules" not in runtime


def test_dockerignore_keeps_the_panel_build_output_out_of_the_context():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("web/node_modules", "web/.next", "web/out", "web/tsconfig.tsbuildinfo"):
        assert pattern in text
