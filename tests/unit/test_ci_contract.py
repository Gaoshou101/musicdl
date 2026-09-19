"""The CI workflow must keep running the suite, the release gates, and the panel build."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare ``on`` key to the boolean True.
    return data, data.get("on") or data.get(True)


def job(data, name):
    return data["jobs"][name]


def commands(job):
    return [step.get("run", "") for step in job["steps"]]


def setup_step(job, action):
    return next(step for step in job["steps"] if str(step.get("uses", "")).startswith(action))


def test_ci_runs_the_suite_and_the_release_gates_on_every_push_and_pull_request():
    data, triggers = workflow()
    assert set(triggers) == {"push", "pull_request"}
    gates = job(data, "release-gates")
    assert gates["runs-on"] == "ubuntu-latest"
    ran = commands(gates)
    assert any("python -m pytest -q" in command for command in ran)
    assert any("scripts/release/run_gates.py --self-test --dry-run" in command for command in ran)


def test_ci_pins_a_supported_python_and_stays_read_only():
    data, _ = workflow()
    setup = setup_step(job(data, "release-gates"), "actions/setup-python")
    assert setup["with"]["python-version"] == "3.12"
    assert data["permissions"] == {"contents": "read"}


def test_ci_reports_the_gates_a_hosted_runner_cannot_decide_instead_of_passing_them():
    data, _ = workflow()
    ran = " ".join(commands(job(data, "release-gates")))
    assert "--dry-run" in ran, "a gate that cannot run must stay NOT_RUN, not become a PASS"
    assert "--wecom-url" not in ran and "--redis-url" not in ran


def test_ci_actions_are_new_enough_to_stop_targeting_node_20():
    """Node 20 actions are deprecated on the runners, so pin the floor, not the tag."""
    data, _ = workflow()
    floor = {"actions/checkout": 5, "actions/setup-python": 6, "actions/setup-node": 5}
    seen = {}
    for name in data["jobs"]:
        for step in job(data, name)["steps"]:
            action, _, ref = str(step.get("uses", "")).partition("@")
            if action in floor:
                seen[action] = int(ref.lstrip("v").split(".")[0])
    assert set(seen) == set(floor), "the workflow must keep using every pinned action"
    for action, minimum in floor.items():
        assert seen[action] >= minimum, f"{action}@{seen[action]} targets Node 20 and is deprecated"


def test_ci_builds_and_then_serves_the_panel_image():
    """A Dockerfile that builds is not yet a container that runs the way Compose will."""
    data, _ = workflow()
    panel = job(data, "admin-panel")
    assert panel["runs-on"] == "ubuntu-latest"
    setup = setup_step(panel, "actions/setup-node")
    assert setup["with"]["node-version"] == "22"
    assert setup["with"]["cache-dependency-path"] == "web/package-lock.json"
    ran = " ".join(commands(panel))
    assert "npm ci" in ran
    assert "npx tsc --noEmit" in ran
    assert "docker build" in ran and "docker/web/Dockerfile" in ran
    # The Compose boundary is a read-only root filesystem whose writable paths
    # are tmpfs, so the smoke run has to reproduce exactly that.
    assert "--read-only" in ran and "/app/.next/cache" in ran
    assert "/healthz" in ran


def test_ci_panel_job_never_needs_a_deployment_secret():
    data, _ = workflow()
    panel = job(data, "admin-panel")
    assert "env" not in panel
    ran = " ".join(commands(panel))
    assert "--build-arg" not in ran, "the image's own default is the Compose service name"
