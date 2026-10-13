"""Lite mode remains available as an option in the unified Compose deployment."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"


def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_lite_mode_is_an_application_override_in_the_unified_stack():
    data = compose()
    services = data["services"]
    app = services["musicdl"]

    assert set(services) == {"musicdl", "plugin-runner", "redis"}
    assert app["environment"]["MUSICDL_DEPLOYMENT_MODE"] == "${MUSICDL_DEPLOYMENT_MODE:-full}"
    assert app["environment"]["MUSICDL_WECOM__ENABLED"] == "${MUSICDL_WECOM__ENABLED:-false}"
    assert app["environment"]["MUSICDL_REDIS__URL"] == "redis://redis:6379/0"
    assert set(app["depends_on"]) == {"redis", "plugin-runner"}


def test_lite_mode_uses_the_app_entrypoint_and_configurable_policy():
    app = compose()["services"]["musicdl"]

    assert app["command"] == [
        "uvicorn", "musicdl.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"
    ]
    assert app["environment"]["MUSICDL_DEPLOYMENT_MODE"] == "${MUSICDL_DEPLOYMENT_MODE:-full}"
    assert app["environment"]["MUSICDL_WECOM__ENABLED"] == "${MUSICDL_WECOM__ENABLED:-false}"


def test_lite_keeps_app_data_and_plugin_sandbox_boundaries():
    data = compose()
    app = data["services"]["musicdl"]
    runner = data["services"]["plugin-runner"]
    redis = data["services"]["redis"]

    assert set(app["volumes"]) == {
        "musicdl-media:/data/music",
        "musicdl-app-data:/data/app",
        "musicdl-telegram:/data/telegram-sessions",
    }
    assert "musicdl-telegram:/data/telegram-sessions" not in runner.get("volumes", [])
    assert runner["networks"] == ["plugin-control"]
    assert data["networks"]["plugin-control"]["internal"] is True
    assert runner.get("environment", {}) == {}
    assert runner.get("volumes", []) == []
    assert runner.get("secrets", []) == []
    assert "ports" not in runner
    assert "ports" not in redis


def test_lite_mode_ci_exercises_the_same_manifest_with_wecom_disabled():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["lite-mode-source-image"]
    script = "\n".join(step.get("run", "") for step in job["steps"])

    assert job["env"]["MUSICDL_DEPLOYMENT_MODE"] == "lite"
    assert job["env"]["MUSICDL_WECOM__ENABLED"] == "false"
    assert "--file compose.yaml" in script
    assert "compose up --detach --no-build --pull missing --wait" in script
    assert "compose config --services" in script
    assert "/healthz" in script and "/readyz" in script
    assert "rejected.status_code != 422" in script
