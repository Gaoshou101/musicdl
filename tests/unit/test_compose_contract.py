from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def compose():
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_one_manifest_runs_release_images_or_builds_from_checkout():
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


def test_compose_main_uses_bundled_redis_and_owns_application_volumes():
    data = compose()
    main = data["services"]["musicdl"]
    plugin = data["services"]["plugin-runner"]
    redis = data["services"]["redis"]

    assert main["environment"]["MUSICDL_REDIS__URL"] == "redis://redis:6379/0"
    assert main["environment"]["MUSICDL_PLUGIN__SERVICE_URL"] == "http://plugin-runner:8080"
    assert main["environment"]["MUSICDL_CONFIG__VERSION"] == "1"
    assert main["environment"]["MUSICDL_MEDIA__ROOT"] == "/data/music"
    assert main["environment"]["MUSICDL_TELEGRAM__SESSION_ROOT"] == "/data/telegram-sessions"
    assert main["environment"]["MUSICDL_DEPLOYMENT_MODE"] == "${MUSICDL_DEPLOYMENT_MODE:-full}"
    assert set(main["volumes"]) == {
        "musicdl-media:/data/music", "musicdl-app-data:/data/app", "musicdl-telegram:/data/telegram-sessions"
    }
    assert redis["volumes"] == ["musicdl-redis-data:/data"]
    assert set(data["volumes"]) == {"musicdl-media", "musicdl-app-data", "musicdl-telegram", "musicdl-redis-data"}
    assert plugin.get("volumes", []) == []


def test_compose_loopback_cookie_and_private_network_defaults():
    data = compose()
    services = data["services"]
    main = services["musicdl"]
    plugin = services["plugin-runner"]
    redis = services["redis"]

    assert main["ports"] == ["${MUSICDL_BIND_HOST:-127.0.0.1}:${MUSICDL_PORT:-8000}:8000"]
    assert main["environment"]["MUSICDL_ADMIN__COOKIE_SECURE"] == "${MUSICDL_ADMIN__COOKIE_SECURE:-false}"
    assert set(main["networks"]) == {"default", "state", "plugin-control"}
    assert plugin["networks"] == ["plugin-control"]
    assert redis["networks"] == ["state"]
    assert data["networks"]["state"]["internal"] is True
    assert data["networks"]["plugin-control"]["internal"] is True
    assert "ports" not in plugin and "ports" not in redis


def test_compose_hardens_each_container_and_waits_for_healthy_services():
    data = compose()
    services = data["services"]

    for service in services.values():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert "healthcheck" in service

    for name in ("musicdl", "plugin-runner"):
        assert services[name]["user"] == "10001:10001"
        assert services[name]["restart"] == "unless-stopped"

    assert services["musicdl"]["depends_on"] == {
        "redis": {"condition": "service_healthy"},
        "plugin-runner": {"condition": "service_healthy"},
    }
    assert services["redis"]["image"] == "redis:8.8.3-alpine"
    assert services["redis"]["restart"] == "unless-stopped"


def test_compose_plugin_runner_has_no_host_integration_surfaces():
    plugin = compose()["services"]["plugin-runner"]

    assert plugin["mem_limit"] == "512m"
    assert plugin["cpus"] == "1.0"
    assert plugin["pids_limit"] == 64
    assert plugin["init"] is True
    assert plugin["tmpfs"] == ["/tmp:size=32m,noexec,nosuid,nodev"]
    assert plugin.get("environment", {}) == {}
    assert plugin.get("volumes", []) == []
    assert plugin.get("secrets", []) == []
    assert "ports" not in plugin


def test_compose_uses_runtime_native_healthchecks_and_quiet_uvicorn():
    data = compose()
    services = data["services"]

    assert all(service["healthcheck"]["test"][0] == "CMD" for service in services.values())
    assert services["musicdl"]["command"][-1] == "--no-access-log"
    assert services["redis"]["command"] == ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]


def test_main_image_initialises_every_compose_volume_mountpoint_for_the_service_uid():
    mounts = {entry.split(":")[1] for entry in compose()["services"]["musicdl"]["volumes"]}
    text = (ROOT / "docker/main/Dockerfile").read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "install -d" in line)

    assert mounts <= set(line.split())
    assert "-o 10001" in line and "-g 10001" in line
    assert text.index("useradd") < text.index("install -d") < text.index("USER 10001:10001")
