"""The lite-mode CI job builds current source and checks the app policy end to end."""

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


def test_lite_ci_builds_both_current_source_images_with_a_unique_tag():
    data, _ = workflow()
    job = data["jobs"]["lite-mode-source-image"]
    script = run_of(job)

    assert job["runs-on"] == "ubuntu-latest"
    assert job["env"]["MUSICDL_DEPLOYMENT_MODE"] == "lite"
    assert job["env"]["MUSICDL_WECOM__ENABLED"] == "false"
    assert job["env"]["MUSICDL_ADMIN__COOKIE_SECURE"] == "false"
    assert job["env"]["MUSICDL_PORT"] == "3997"
    assert job["env"]["MUSICDL_IMAGE_TAG"] == "ci-lite-${{ github.run_id }}-${{ github.run_attempt }}"
    assert 'project="musicdl-lite-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in script
    assert 'docker compose --project-name "$project" --file compose.yaml' in script
    assert script.count("compose build musicdl plugin-runner") == 1
    assert "compose up --detach --no-build --pull missing --wait --wait-timeout 120" in script
    assert "compose pull" not in script


def test_lite_ci_starts_the_bundle_and_checks_the_app_lite_contract():
    data, _ = workflow()
    script = run_of(data["jobs"]["lite-mode-source-image"])

    expected = 'expected_services="$(printf \'%s\\n\' musicdl plugin-runner redis | sort)"'
    assert expected in script
    assert "compose config --services" in script
    assert "compose ps --services --status running" in script
    assert "/healthz" in script and "/readyz" in script
    assert "http://127.0.0.1:3997/" in script and "管理后台" in script
    assert "/admin/config" in script and 'test "$status" = "401"' in script
    assert "compose logs --no-color --tail=80" in script
    assert "compose down --volumes --remove-orphans --timeout 20" in script
    assert "rejected.status_code != 422" in script
    assert '"wecom.enabled": True' in script


def test_lite_ci_python_blocks_parse_and_keep_credentials_redacted():
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
    assert 'client.post("/admin/login"' in blocks[1]
    assert 'client.patch(' in blocks[1] and '"wecom.enabled": True' in blocks[1]
    assert 'setting(after.json(), "wecom.enabled") is not False' in blocks[1]
    assert 'saved.get("settings") != {}' in blocks[1]
    assert "credential values redacted" in blocks[0]


def test_lite_ci_keeps_the_release_gate_and_default_published_stack_smoke():
    data, _ = workflow()
    assert "release-gates" in data["jobs"]
    assert any(
        "scripts/release/run_gates.py --self-test --dry-run" in step.get("run", "")
        for step in data["jobs"]["release-gates"]["steps"]
    )
    published = run_of(data["jobs"]["quick-install-stack"])
    assert "compose.yaml" in published
    assert "compose pull" in published
