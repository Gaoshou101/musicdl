"""The root Compose file is the published-image install and source-build entry point."""

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"


def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def fenced_blocks(markdown, language):
    return re.findall(rf"(?ms)^```{re.escape(language)}\s*\n(.*?)^```\s*$", markdown)


def test_install_uses_the_release_images_and_also_supports_source_builds():
    data = compose()
    services = data["services"]

    assert data["name"] == "musicdl"
    assert set(services) == {"musicdl", "plugin-runner", "redis"}
    assert services["musicdl"]["image"] == "wit7zz/musicdl:${MUSICDL_IMAGE_TAG:-1.1.5}"
    assert services["plugin-runner"]["image"] == "wit7zz/musicdl-plugin-runner:${MUSICDL_IMAGE_TAG:-1.1.5}"
    assert services["musicdl"]["build"] == {"context": ".", "dockerfile": "docker/main/Dockerfile"}
    assert services["plugin-runner"]["build"] == {"context": ".", "dockerfile": "docker/plugin/Dockerfile"}
    assert services["musicdl"]["env_file"] == [{"path": ".env", "required": False}]
    assert not any((ROOT / name).exists() for name in ("compose.quick.yaml", "compose.prod.yaml", "compose.lite.yaml"))


def test_install_defaults_are_documented_and_local_http_login_works():
    services = compose()["services"]
    app = services["musicdl"]

    assert app["ports"] == ["${MUSICDL_BIND_HOST:-127.0.0.1}:${MUSICDL_PORT:-8000}:8000"]
    assert app["environment"]["MUSICDL_ADMIN__COOKIE_SECURE"] == "${MUSICDL_ADMIN__COOKIE_SECURE:-false}"
    assert app["environment"]["MUSICDL_DEPLOYMENT_MODE"] == "${MUSICDL_DEPLOYMENT_MODE:-full}"
    assert app["environment"]["MUSICDL_WECOM__ENABLED"] == "${MUSICDL_WECOM__ENABLED:-false}"

    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for readme in (english, chinese):
        assert "https://raw.githubusercontent.com/Gaoshou101/musicdl/main/compose.yaml" in readme
        assert "MUSICDL_ADMIN__COOKIE_SECURE" in readme
        assert "musicdl-redis-data" in readme
        assert re.search(r"(?m)^\| `MUSICDL_IMAGE_TAG` \| `1\.1\.5` \|", readme)
    assert "MUSICDL_IMAGE_TAG=1.1.5" in env_example
    assert "MUSICDL_BIND_HOST=127.0.0.1" in env_example
    assert "MUSICDL_ADMIN__COOKIE_SECURE=false" in env_example
    assert "MUSICDL_DEPLOYMENT_MODE=full" in env_example


def test_install_bundles_private_persistent_redis_and_keeps_app_volumes():
    data = compose()
    services = data["services"]
    app = services["musicdl"]
    redis = services["redis"]

    assert app["environment"]["MUSICDL_REDIS__URL"] == "redis://redis:6379/0"
    assert app["volumes"] == [
        "musicdl-media:/data/music",
        "musicdl-app-data:/data/app",
        "musicdl-telegram:/data/telegram-sessions",
    ]
    assert redis["image"] == "redis:8.8.3-alpine"
    assert redis["volumes"] == ["musicdl-redis-data:/data"]
    assert "ports" not in redis
    assert set(data["volumes"]) == {
        "musicdl-media", "musicdl-app-data", "musicdl-telegram", "musicdl-redis-data"
    }
    assert "MUSICDL_REDIS__URL" not in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_migration_guidance_covers_separate_redis_and_volume_mapping():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    for readme in (english, chinese):
        for volume in ("musicdl-media", "musicdl-app-data", "musicdl-telegram", "musicdl-redis-data"):
            assert f"| `{volume}` |" in readme
        assert "docker compose down -v" in readme


def test_source_build_uses_a_local_tag_then_starts_without_rebuilding_or_forcing_pulls():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    compose_text = COMPOSE.read_text(encoding="utf-8")

    for readme in (english, chinese):
        bash_blocks = fenced_blocks(readme, "bash")
        powershell_blocks = fenced_blocks(readme, "powershell")
        assert any("docker compose up -d" in block and "compose.yaml" in block for block in bash_blocks)
        assert any("MUSICDL_IMAGE_TAG=local" in block and "docker compose build musicdl plugin-runner" in block
                   and "docker compose up -d --no-build --pull missing" in block for block in bash_blocks)
        assert any("MUSICDL_IMAGE_TAG = \"local\"" in block and "docker compose build musicdl plugin-runner" in block
                   and "docker compose up -d --no-build --pull missing" in block for block in powershell_blocks)
    assert "--no-build --pull missing" in compose_text
