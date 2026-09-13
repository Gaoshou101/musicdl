from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def compose():
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_has_exactly_two_application_services_and_no_redis_service():
    data = compose()
    assert set(data["services"]) == {"musicdl", "plugin-runner"}
    assert all("redis" not in str(service.get("image", "")).lower() for service in data["services"].values())


def test_compose_main_owns_volumes_and_only_receives_external_redis_url():
    data = compose()
    main = data["services"]["musicdl"]
    plugin = data["services"]["plugin-runner"]
    assert main["environment"]["MUSICDL_REDIS__URL"] == "${MUSICDL_REDIS__URL:?set an external Redis URL}"
    assert not any("REDIS" in str(v).upper() for v in plugin.get("environment", {}).keys())
    assert set(main["volumes"]) == {"musicdl-media:/data/music", "musicdl-app-data:/data/app", "musicdl-telegram:/data/telegram-sessions"}
    assert plugin.get("volumes", []) == []
    assert main["environment"]["MUSICDL_PLUGIN__SERVICE_URL"] == "http://plugin-runner:8080"
    assert main["environment"]["MUSICDL_CONFIG__VERSION"] == "1"
    assert main["environment"]["MUSICDL_MEDIA__ROOT"] == "/data/music"
    assert main["environment"]["MUSICDL_TELEGRAM__SESSION_ROOT"] == "/data/telegram-sessions"


def test_compose_plugin_runner_is_internal_and_main_can_reach_it():
    data = compose()
    main = data["services"]["musicdl"]
    plugin = data["services"]["plugin-runner"]
    assert set(main["networks"]) == {"default", "plugin-control"}
    assert plugin["networks"] == ["plugin-control"]
    assert data["networks"]["plugin-control"]["internal"] is True


def test_compose_security_and_network_boundaries():
    data = compose()
    main = data["services"]["musicdl"]
    plugin = data["services"]["plugin-runner"]
    for service in (main, plugin):
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert any(str(tmpfs).startswith("/tmp") for tmpfs in service["tmpfs"])
    assert main["ports"] == ["127.0.0.1:${MUSICDL_PORT:-8000}:8000"]
    assert "ports" not in plugin


def test_compose_plugin_runner_resource_limits_are_exact():
    plugin = compose()["services"]["plugin-runner"]
    assert plugin["mem_limit"] == "512m"
    assert plugin["cpus"] == "1.0"
    assert plugin["pids_limit"] == 64
    assert plugin["init"] is True
    assert plugin["tmpfs"] == ["/tmp:size=32m,noexec,nosuid,nodev"]


def test_compose_plugin_runner_has_no_host_integration_surfaces():
    plugin = compose()["services"]["plugin-runner"]
    assert plugin.get("secrets", []) == []
    assert not any("docker.sock" in str(v) for v in plugin.get("volumes", []))
    assert not any(key.upper().startswith(("REDIS", "OPENAI", "TELEGRAM")) for key in plugin.get("environment", {}))


def test_compose_uses_pinned_images_and_stdlib_healthchecks():
    data = compose()
    for service in data["services"].values():
        assert service["build"]["context"] == "."
        assert service["healthcheck"]["test"][0] == "CMD"
        assert service["healthcheck"]["test"][1] == "python"
    assert data["services"]["musicdl"]["build"]["dockerfile"] == "docker/main/Dockerfile"
    assert data["services"]["plugin-runner"]["build"]["dockerfile"] == "docker/plugin/Dockerfile"


def test_compose_disables_uvicorn_query_string_access_logs():
    command = compose()["services"]["musicdl"]["command"]
    assert "--no-access-log" in command
