"""Every version bump has to ship the notes and the workflow that publishes them.

A tag is only half of a release. ``docker-publish.yml`` creates the GitHub Release
from ``docs/release-notes/v<version>.md`` once the images are pushed, so nothing
else catches a version bump that forgot the notes, and nothing else catches a
release job that stopped reading them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
NOTES_DIR = ROOT / "docs" / "release-notes"


def version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["version"]


def workflow() -> dict:
    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))


def test_the_version_being_released_has_release_notes():
    current = version()
    notes = NOTES_DIR / f"v{current}.md"
    assert notes.is_file(), f"{notes.relative_to(ROOT)} is required by the v{current} release"

    text = notes.read_text(encoding="utf-8")
    assert f"# musicdl {current}" in text
    assert "## English" in text and "## 中文" in text
    # The workflow appends the provenance, so a hand-written copy here would be a
    # second answer to a question that only the pushed images can answer.
    assert "Source commit:" not in text
    assert "Docker images" not in text


def test_the_publish_workflow_builds_the_release_from_those_notes():
    data = workflow()
    assert data["permissions"] == {"contents": "read"}
    release = data["jobs"]["release"]
    assert release["needs"] == "publish"
    assert release["permissions"] == {"contents": "write"}
    assert "tag" in str(release["if"]), "only a tag push has a release to publish"
    run = "\n".join(step.get("run", "") for step in release["steps"])
    assert "docs/release-notes/${tag}.md" in run
    assert "required release notes" in run
    assert "releases/generate-notes" not in run
    assert "gh release create" in run and "gh release edit" in run
    assert "--notes-file" in run


def test_the_publish_job_hands_the_release_its_version_and_both_digests():
    data = workflow()
    outputs = data["jobs"]["publish"]["outputs"]
    assert set(outputs) == {"version", "main", "runner"}
    assert all(value.startswith("${{ steps.") for value in outputs.values())
    for step_id in ("main", "runner"):
        step = next(step for step in data["jobs"]["publish"]["steps"] if step.get("id") == step_id)
        assert step["uses"].startswith("docker/build-push-action@")


def test_manual_publish_cannot_overwrite_a_numbered_release_image_without_updating_its_release():
    data = workflow()
    dispatch = data.get("on", data.get(True))["workflow_dispatch"]
    assert not dispatch or not dispatch.get("inputs")
    tag_step = next(step for step in data["jobs"]["publish"]["steps"] if step.get("id") == "tag")
    assert "value=latest" in tag_step["run"]
    assert "inputs.tag" not in tag_step["run"]
