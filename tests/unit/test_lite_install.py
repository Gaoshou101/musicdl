from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
LITE_COMPOSE = ROOT / "compose.lite.yaml"


def lite_compose():
    return yaml.safe_load(LITE_COMPOSE.read_text(encoding="utf-8"))


def test_lite_install_builds_exactly_two_services_from_current_source():
    data = lite_compose()
    services = data["services"]

    assert data["name"] == "musicdl"
    assert set(services) == {"musicdl", "plugin-runner"}
    assert all("image" not in service for service in services.values())
    assert services["musicdl"]["build"] == {
        "context": ".",
        "dockerfile": "docker/main/Dockerfile",
    }
    assert services["plugin-runner"]["build"] == {
        "context": ".",
        "dockerfile": "docker/plugin/Dockerfile",
    }
    assert "redis" not in services
    assert set(data["volumes"]) == {
        "musicdl-media",
        "musicdl-app-data",
        "musicdl-telegram",
    }
    assert "state" not in data["networks"]


def test_lite_install_preserves_app_data_volumes_without_a_redis_dependency():
    data = lite_compose()
    app = data["services"]["musicdl"]

    assert set(app["volumes"]) == {
        "musicdl-media:/data/music",
        "musicdl-app-data:/data/app",
        "musicdl-telegram:/data/telegram-sessions",
    }
    assert app["environment"]["MUSICDL_DEPLOYMENT_MODE"] == "lite"
    assert app["environment"]["MUSICDL_WECOM__ENABLED"] == "false"
    assert not any(key.upper().startswith("MUSICDL_REDIS") for key in app["environment"])
    assert app["environment"]["MUSICDL_PLUGIN__SERVICE_URL"] == "http://plugin-runner:8080"
    assert app["environment"]["MUSICDL_ADMIN__COOKIE_SECURE"] == "${MUSICDL_ADMIN__COOKIE_SECURE:-true}"
    assert app["ports"] == ["127.0.0.1:${MUSICDL_PORT:-8000}:8000"]


def test_lite_install_fails_closed_before_uvicorn_on_an_unsupported_or_full_image():
    app = lite_compose()["services"]["musicdl"]
    command = "\n".join(app["command"])

    assert app["command"][:2] == ["sh", "-ec"]
    assert command.index("python - <<'PY'") < command.index("exec uvicorn")
    assert '"deployment_mode" not in getattr(AppSettings, "model_fields", {})' in command
    assert "AppSettings.deployment_mode" in command
    assert 'settings.deployment_mode != "lite"' in command
    assert "MUSICDL_DEPLOYMENT_MODE=lite" in command


def test_lite_install_keeps_the_full_mode_plugin_sandbox_without_secrets():
    data = lite_compose()
    app = data["services"]["musicdl"]
    runner = data["services"]["plugin-runner"]
    quick_runner = yaml.safe_load((ROOT / "compose.quick.yaml").read_text(encoding="utf-8"))["services"]["plugin-runner"]

    assert set(app["networks"]) == {"default", "plugin-control"}
    assert runner["networks"] == ["plugin-control"]
    assert data["networks"]["plugin-control"]["internal"] is True
    assert runner.get("environment", {}) == {}
    assert runner.get("volumes", []) == []
    assert runner.get("secrets", []) == []
    assert "ports" not in runner
    for key in ("user", "read_only", "cap_drop", "security_opt", "mem_limit", "cpus", "pids_limit", "init", "tmpfs"):
        assert runner[key] == quick_runner[key]


def test_lite_install_docs_explain_build_mode_and_full_to_lite_rollback_boundaries():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    english_lite = english.split("## Lite Two-Container Deployment", 1)[1].split("## Existing Repository Deployment", 1)[0]
    chinese_lite = chinese.split("## Lite 双容器部署", 1)[1].split("## 现有仓库部署方式", 1)[0]

    for text in (english_lite, chinese_lite):
        assert "compose.lite.yaml" in text
        assert "MUSICDL_DEPLOYMENT_MODE" in text
        assert "docker/plugin/Dockerfile" in text
    assert "published `1.0.3` main and plugin-runner image tags support lite" in english_lite.lower()
    assert "compose.lite.yaml` still builds both images from the source checkout" in english_lite
    assert "已发布的 `1.0.3` 主服务和插件运行器镜像标签均支持 lite" in chinese_lite
    assert "compose.lite.yaml` 仍会从源码检出目录" in chinese_lite

    english_migration = english.split("### Switching between full and lite mode", 1)[1].split("## Configuration", 1)[0]
    chinese_migration = chinese.split("### 在 full 与 lite 模式之间切换", 1)[1].split("## 配置", 1)[0]
    for text in (english_migration, chinese_migration):
        assert "down -v" not in text
        assert "musicdl-media" in text
        assert "musicdl-app-data" in text
        assert "musicdl-telegram" in text
        assert "Redis" in text or "redis" in text
        assert "rollback" in text.lower() or "回滚" in text

    assert "disable WeCom and save that change while still in full mode" in english_migration
    assert "inspect Runtime Configuration → Redis → Redis Address" in english_migration
    assert "panel override" in english_migration.lower()
    assert "service-only rollback" in english_migration
    assert "Lite performs no Redis writes" in english_migration
    assert "在 full 模式下先禁用企业微信并保存" in chinese_migration
    assert "检查 Redis 配置来源" in chinese_migration
    assert "面板覆盖" in chinese_migration
    assert "仅回滚服务" in chinese_migration
    assert "lite 不会写入 Redis" in chinese_migration


def test_release_docs_pin_current_images_and_document_quick_default():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for text in (english, chinese):
        assert "1.0.3" in text
        assert "MUSICDL_IMAGE_TAG=1.0.3" in text
    assert "wit7zz/musicdl:1.0.3" in english
    assert "wit7zz/musicdl-plugin-runner:1.0.3" in english
    assert "`wit7zz/musicdl:1.0.3`" in chinese
    assert "`wit7zz/musicdl-plugin-runner:1.0.3`" in chinese
    assert "quick manifest now defaults to `1.0.3`" in english
    assert "快速安装清单现在默认使用 `1.0.3`" in chinese
    assert "still defaults to `1.0.2` until post-publication promotion" not in english
    assert "发布后完成推广前仍默认使用" not in chinese
    assert "MUSICDL_IMAGE_TAG=1.0.3" in env_example
    assert "1.0.1" in env_example and "v1.0.0" in env_example
    assert "python-socks" in english and "python-socks" in chinese
