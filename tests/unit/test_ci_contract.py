"""The CI workflow must keep running the suite and the release gates."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare ``on`` key to the boolean True.
    return data, data.get("on") or data.get(True)


def single_job(data):
    (job,) = data["jobs"].values()
    return job


def commands(job):
    return [step.get("run", "") for step in job["steps"]]


def test_ci_runs_the_suite_and_the_release_gates_on_every_push_and_pull_request():
    data, triggers = workflow()
    assert set(triggers) == {"push", "pull_request"}
    job = single_job(data)
    assert job["runs-on"] == "ubuntu-latest"
    ran = commands(job)
    assert any("python -m pytest -q" in command for command in ran)
    assert any("scripts/release/run_gates.py --self-test --dry-run" in command for command in ran)


def test_ci_pins_a_supported_python_and_stays_read_only():
    data, _ = workflow()
    job = single_job(data)
    setup = next(step for step in job["steps"]
                 if str(step.get("uses", "")).startswith("actions/setup-python"))
    assert setup["with"]["python-version"] == "3.12"
    assert data["permissions"] == {"contents": "read"}


def test_ci_reports_the_gates_a_hosted_runner_cannot_decide_instead_of_passing_them():
    data, _ = workflow()
    ran = " ".join(commands(single_job(data)))
    assert "--dry-run" in ran, "a gate that cannot run must stay NOT_RUN, not become a PASS"
    assert "--wecom-url" not in ran and "--redis-url" not in ran
