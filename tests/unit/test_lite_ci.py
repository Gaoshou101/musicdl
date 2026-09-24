"""The lite-mode CI smoke test must exercise current-source images end to end."""

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data, data.get("on") or data.get(True)


def run_of(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_lite_ci_builds_both_current_source_images_once_in_a_unique_project():
    data, _ = workflow()
    job = data["jobs"]["lite-mode-source-image"]
    script = run_of(job)

    assert job["runs-on"] == "ubuntu-latest"
    assert job["env"]["MUSICDL_DEPLOYMENT_MODE"] == "lite"
    assert job["env"]["MUSICDL_ADMIN__COOKIE_SECURE"] == "false"
    assert job["env"]["MUSICDL_PORT"] == "3997"
    assert 'project="musicdl-lite-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in script
    assert 'docker compose --project-name "$project" --file compose.lite.yaml' in script
    assert script.count("compose build musicdl plugin-runner") == 1
    assert "compose up --detach --no-build --wait --wait-timeout 120" in script
    assert "compose pull" not in script, "lite mode has no compatible published image yet"
    assert "MUSICDL_IMAGE_TAG" not in script


def test_lite_ci_waits_for_exactly_two_services_and_checks_health_panel_and_auth():
    data, _ = workflow()
    script = run_of(data["jobs"]["lite-mode-source-image"])

    assert "compose config --services" in script
    assert "musicdl plugin-runner" in script
    assert "redis" not in script.lower()
    assert "compose ps --services --status running" in script
    assert "/healthz" in script and "/readyz" in script
    assert "http://127.0.0.1:3997/" in script and "管理后台" in script
    assert "/admin/config" in script and 'test "$status" = "401"' in script
    assert "compose logs --no-color --tail=80" in script
    assert "compose down --volumes --remove-orphans --timeout 20" in script


def test_lite_ci_authenticates_without_printing_credentials_and_checks_no_saved_change():
    data, _ = workflow()
    script = run_of(data["jobs"]["lite-mode-source-image"])
    blocks = re.findall(r"python - <<'PY'(?: \|\| fail)?\n(.*?)\nPY\n", script, re.DOTALL)

    assert len(blocks) == 2
    for block in blocks:
        ast.parse(block)
        assert "LITE_CI_PASSWORD" in block
        assert "print(" not in block or "PASSWORD" not in block.split("print(", 1)[1].split(")", 1)[0]

    assert "PasswordHasher().hash(os.environ[\"LITE_CI_PASSWORD\"])" in blocks[0]
    assert "settings={}" in blocks[0]
    policy = blocks[1]
    assert 'client.post("/admin/login"' in policy
    assert 'client.patch(' in policy and '"wecom.enabled": True' in policy
    assert "rejected.status_code != 422" in policy
    assert 'setting(after.json(), "wecom.enabled") is not False' in policy
    assert 'saved.get("settings") != {}' in policy
    assert "credential values redacted" in blocks[0]


def test_lite_ci_keeps_the_release_gate_and_published_quick_install_checks():
    data, _ = workflow()

    assert "release-gates" in data["jobs"]
    assert any(
        "scripts/release/run_gates.py --self-test --dry-run" in step.get("run", "")
        for step in data["jobs"]["release-gates"]["steps"]
    )
    quick = run_of(data["jobs"]["quick-install-stack"])
    assert "compose.quick.yaml" in quick
    assert "compose pull" in quick
    assert "plugin-security" not in quick
