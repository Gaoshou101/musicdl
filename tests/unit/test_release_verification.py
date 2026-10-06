"""Release checks reject drift and missing evidence without contacting registries."""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("verify_release", ROOT / "scripts/release/verify_release.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
TAG = f"v{VERSION}"
SHA = "a" * 40
OTHER = "b" * 40
NOTES = (ROOT / f"docs/release-notes/{TAG}.md").read_text(encoding="utf-8")


@pytest.fixture
def release_tree(tmp_path):
    for name in ("pyproject.toml", "docker/plugin/pyproject.toml", "web/package.json",
                 "web/package-lock.json", "docker/main/Dockerfile", "docker/plugin/Dockerfile",
                 ".env.example", "compose.quick.yaml", "compose.prod.yaml", "README.md", "README.zh-CN.md",
                 f"docs/release-notes/{TAG}.md"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    return tmp_path


def git(*, tag_type="tag", commit=SHA, refs=None, notes=NOTES, metadata_diff=""):
    def run(args):
        if args[1:3] == ["rev-parse", "HEAD"]:
            return SHA
        if args[1] == "for-each-ref":
            return f"refs/tags/{TAG}" if refs is None else refs
        if args[1] == "cat-file":
            return tag_type
        if args[1] == "rev-parse":
            return commit
        if args[1] == "show":
            return notes
        if args[1] == "diff":
            assert args[2:6] == ["--name-only", "HEAD", "--", "pyproject.toml"]
            return metadata_diff
        raise AssertionError(args)
    return run


def test_normal_preflight_and_annotated_tag(release_tree):
    assert verify.preflight(release_tree, run=git()).exit_code() == 0
    assert verify.preflight(release_tree, TAG, git()).exit_code() == 0


@pytest.mark.parametrize("name", ["docker/plugin/pyproject.toml", "web/package.json", "web/package-lock.json",
                                   "docker/main/Dockerfile", "docker/plugin/Dockerfile", ".env.example",
                                   "compose.quick.yaml", "README.md", "README.zh-CN.md"])
def test_version_drift_fails(release_tree, name):
    path = release_tree / name
    path.write_text(path.read_text(encoding="utf-8").replace(VERSION, "99.0.0"), encoding="utf-8")
    assert verify.preflight(release_tree, run=git()).exit_code() == 1


def test_lock_root_version_is_checked_separately(release_tree):
    path = release_tree / "web/package-lock.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["packages"][""]["version"] = "99.0.0"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert verify.preflight(release_tree, run=git()).exit_code() == 1


@pytest.mark.parametrize("notes", ["", "# wrong\n", NOTES.replace("## 中文", "## Chinese"), NOTES + "\nSource commit: fake\n"])
def test_notes_shape_and_handwritten_provenance_fail(release_tree, notes):
    (release_tree / f"docs/release-notes/{TAG}.md").write_text(notes, encoding="utf-8")
    assert verify.preflight(release_tree, TAG, git()).exit_code() == 1


def test_missing_notes_with_existing_tag_does_not_crash(release_tree):
    (release_tree / f"docs/release-notes/{TAG}.md").unlink()
    assert verify.preflight(release_tree, TAG, git()).exit_code() == 1


@pytest.mark.parametrize("tag,run", [(TAG, git(tag_type="commit")), (TAG, git(commit=OTHER)),
                                     (TAG, git(refs="")), ("v99.0.0", git()), (TAG, git(notes=NOTES + "drift"))])
def test_wrong_tag_or_committed_notes_fails(release_tree, tag, run):
    assert verify.preflight(release_tree, tag, run).exit_code() == 1


def test_history_example_is_not_a_current_recommendation(release_tree):
    path = release_tree / "README.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nHistorical rollback: MUSICDL_IMAGE_TAG=0.9.0\n", encoding="utf-8")
    assert verify.preflight(release_tree, run=git()).exit_code() == 0


def test_no_tag_preflight_allows_dirty_metadata_but_tagged_does_not(release_tree):
    run = git(metadata_diff="web/package.json")
    assert verify.preflight(release_tree, run=run).exit_code() == 0
    assert verify.preflight(release_tree, TAG, run).exit_code() == 1


@pytest.mark.parametrize("staged", [False, True])
def test_local_fix_cannot_mask_tagged_metadata_drift(release_tree, staged):
    def repo_git(*args):
        return subprocess.run(["git", "-c", "user.name=Release Test", "-c", "user.email=release-test@example.invalid", *args],
                              cwd=release_tree, capture_output=True, text=True, check=True)
    path = release_tree / "docker/plugin/pyproject.toml"
    correct = path.read_text(encoding="utf-8")
    path.write_text(correct.replace(VERSION, "99.0.0"), encoding="utf-8")
    repo_git("init")
    repo_git("add", ".")
    repo_git("commit", "-m", "fixture")
    repo_git("tag", "-a", TAG, "-m", "fixture")
    path.write_text(correct, encoding="utf-8")
    if staged:
        repo_git("add", "docker/plugin/pyproject.toml")
    report = verify.preflight(release_tree, TAG)
    checks = {item["id"]: item["status"] for item in report.checks}
    assert checks["plugin-version"] == "PASS"
    assert checks["tag-commit"] == "PASS"
    assert checks["committed-metadata"] == "FAIL"
    assert report.exit_code() == 1


def run_evidence(workflow="CI", branch="main", **changes):
    return {"workflowName": workflow, "headSha": SHA, "headBranch": branch, "event": "push",
            "status": "completed", "conclusion": "success", "createdAt": "2026-10-06T00:00:00Z",
            "databaseId": 1, **changes}


def image_evidence(number):
    descriptors, configs, provenance = [], {}, {}
    for index, platform in enumerate(verify.PLATFORMS):
        os_name, arch = platform.split("/")
        digest = f"sha256:{number + index + 10:064x}"
        descriptors.extend([
            {"digest": digest, "platform": {"os": os_name, "architecture": arch}},
            {"digest": f"sha256:{number + index + 20:064x}", "platform": {"os": "unknown", "architecture": "unknown"},
             "annotations": {"vnd.docker.reference.digest": digest, "vnd.docker.reference.type": "attestation-manifest"}},
        ])
        configs[platform] = {"os": os_name, "architecture": arch, "config": {"Labels": {
            "org.opencontainers.image.revision": SHA, "org.opencontainers.image.version": VERSION,
            "org.opencontainers.image.source": f"https://github.com/{verify.REPO}"}}}
        source = {"uri": f"https://github.com/{verify.REPO}.git#{SHA}", "digest": {"sha1": SHA}}
        provenance[platform] = {"SLSA": {"invocation": {"configSource": source}, "materials": [copy.deepcopy(source)]}}
    return {"manifest": {"digest": f"sha256:{number:064x}", "mediaType": "application/vnd.oci.image.index.v1+json",
                         "manifests": descriptors}, "configs": configs, "provenance": provenance}


@pytest.fixture
def published():
    images = {name: image_evidence(i + 1) for i, name in enumerate(verify.IMAGES)}
    return {"release": {"tagName": TAG, "name": f"musicdl {VERSION}", "isDraft": False, "isPrerelease": False,
                        "body": verify.release_body(NOTES, TAG, SHA, *(images[n]["manifest"]["digest"] for n in verify.IMAGES))},
            "images": images, "ci": [run_evidence()], "publish": [run_evidence("Publish Docker images", TAG)]}


def checked(evidence):
    report = verify.Report("published", TAG)
    report.source_commit = SHA
    verify.check_published(report, evidence, NOTES, TAG)
    return report


def test_complete_published_evidence_passes(published):
    assert checked(published).exit_code() == 0
    published["release"]["body"] = published["release"]["body"].replace("\n", "\r\n") + "\r\n"
    assert checked(published).exit_code() == 0


@pytest.mark.parametrize("field,value", [("isDraft", True), ("isPrerelease", True), ("isDraft", 0), ("tagName", "v99.0.0"),
                                         ("name", "wrong"), ("body", "rewritten release")])
def test_release_drift_fails(published, field, value):
    published["release"][field] = value
    assert checked(published).exit_code() == 1


def test_body_trailing_spaces_are_not_silently_normalized(published):
    published["release"]["body"] = published["release"]["body"].rstrip("\n") + " \n"
    assert checked(published).exit_code() == 1


@pytest.mark.parametrize("image", verify.IMAGES)
@pytest.mark.parametrize("tamper", ["digest", "platform", "revision", "version", "source", "config-platform",
                                    "primary-source", "material", "attestation", "type"])
def test_each_image_platform_and_source_is_verified(published, image, tamper):
    evidence = published["images"][image]
    arm = "linux/arm64"
    if tamper == "digest":
        evidence["manifest"]["digest"] = "sha256:" + "f" * 64
    elif tamper == "platform":
        evidence["manifest"]["manifests"][2]["platform"] = {"os": "unknown", "architecture": "unknown"}
    elif tamper in {"revision", "version", "source"}:
        evidence["configs"][arm]["config"]["Labels"][f"org.opencontainers.image.{tamper}"] = "wrong"
    elif tamper == "config-platform":
        evidence["configs"][arm]["architecture"] = "amd64"
    elif tamper == "primary-source":
        evidence["provenance"][arm]["SLSA"]["invocation"]["configSource"]["digest"]["sha1"] = OTHER
    elif tamper == "material":
        evidence["provenance"][arm]["SLSA"]["materials"][0]["uri"] = "https://github.com/other/repo.git"
    elif tamper == "attestation":
        evidence["manifest"]["manifests"][3]["annotations"]["vnd.docker.reference.digest"] = "sha256:" + "f" * 64
    else:
        evidence["manifest"]["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
    assert checked(published).exit_code() == 1


@pytest.mark.parametrize("changes", [{"headSha": OTHER}, {"headBranch": "other"}, {"event": "pull_request"},
                                     {"workflowName": "other"}, {"status": "in_progress", "conclusion": ""},
                                     {"conclusion": "failure"}])
@pytest.mark.parametrize("key", ["ci", "publish"])
def test_workflow_identity_and_terminal_success_are_required(published, changes, key):
    published[key][0].update(changes)
    assert checked(published).exit_code() == 1


def test_newer_failed_run_cannot_hide_behind_previous_success(published):
    published["ci"].append(run_evidence(databaseId=2, createdAt="2026-10-06T01:00:00Z", conclusion="failure"))
    assert checked(published).exit_code() == 1


@pytest.mark.parametrize("malformed", [None, [], {"release": None}, {"images": {}}])
def test_malformed_evidence_cannot_pass_or_traceback(malformed):
    assert checked(malformed).exit_code() == 1


@pytest.mark.parametrize("uri", ["https://github.com/other/repo.git", f"https://github.com/{verify.REPO}.git#{OTHER}",
                                 f"https://user@github.com/{verify.REPO}.git", f"https://github.com/{verify.REPO}.git?foo=bar"])
def test_primary_git_source_is_exact(uri):
    assert not verify.git_source({"uri": uri, "digest": {"sha1": SHA}}, SHA, verify.REPO)


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("docker", 60)])
def test_missing_cli_or_timeout_is_unavailable(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(verify.subprocess, "run", fail)
    with pytest.raises(verify.Unavailable):
        verify.command(["docker"])


def test_cli_failure_does_not_leak_stderr(monkeypatch):
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stderr="secret", stdout=""))
    with pytest.raises(verify.Unavailable, match="exit 1") as error:
        verify.command(["gh"])
    assert "secret" not in str(error.value)


def test_malformed_cli_json_is_unavailable():
    with pytest.raises(verify.Unavailable):
        verify.json_command(["docker"], lambda args: "not JSON")


def test_unavailable_source_ci_exits_two():
    report = verify.Report("preflight")
    report.source_commit = SHA
    def fail(args):
        raise verify.Unavailable("gh unavailable")
    verify.require_ci(report, verify.REPO, fail)
    assert report.exit_code() == 2
    report.check("mismatch", False, True)
    assert report.exit_code() == 1


@pytest.mark.parametrize("runs", [None, {}, [None], [{"headSha": SHA, "event": "push", "headBranch": "main",
                                                  "workflowName": "CI", "createdAt": None, "databaseId": "bad"}]])
def test_malformed_run_evidence_fails(runs):
    report = verify.Report("preflight")
    verify.check_run(report, "ci", runs, SHA, workflow="CI", event="push", branch="main")
    assert report.exit_code() == 1


def test_cli_structured_exit_and_no_fixture_override(monkeypatch, capsys):
    report = verify.Report("preflight")
    report.unavailable("tool", "missing")
    monkeypatch.setattr(verify, "preflight", lambda **kwargs: report)
    assert verify.main(["preflight", "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1 and data["status"] == "NOT_RUN"
    with pytest.raises(SystemExit):
        verify.main(["published", "--tag", TAG, "--fixture", "fake.json"])


def test_body_cli_uses_shared_formatter(monkeypatch, capsys):
    monkeypatch.setattr(verify, "ROOT", ROOT)
    main_digest, runner_digest = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    assert verify.main(["body", "--tag", TAG, "--source-commit", SHA,
                        "--main-digest", main_digest, "--runner-digest", runner_digest]) == 0
    assert capsys.readouterr().out == verify.release_body(NOTES, TAG, SHA, main_digest, runner_digest)


def test_registry_reads_are_pinned_after_initial_tag_inspection(published):
    calls = []
    def run(args):
        calls.append(args)
        if args[0] == "gh":
            if args[1] == "release":
                return json.dumps(published["release"])
            return json.dumps(published["ci"] if "ci.yml" in args else published["publish"])
        ref = args[4]
        image = ref.split("@", 1)[0] if "@" in ref else ref.rsplit(":", 1)[0]
        key = {"{{json .Manifest}}": "manifest", "{{json .Image}}": "configs", "{{json .Provenance}}": "provenance"}[args[-1]]
        return json.dumps(published["images"][image][key])
    assert checked(verify.collect_published(TAG, SHA, verify.REPO, run)).exit_code() == 0
    for args in calls:
        if args[0] == "docker" and args[-1] != "{{json .Manifest}}":
            assert "@sha256:" in args[4]


def test_ci_keeps_frontend_regressions_and_production_build():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    steps = jobs["admin-panel"]["steps"]
    runs = [step.get("run", "") for step in steps]
    for name in ("service-logs", "search-probe", "source-import", "channel-metrics"):
        assert f"npm run check:{name}" in runs
    assert any("compose.prod.yaml build musicdl" in run for run in runs)
    assert any("verify_release.py preflight --json" in s.get("run", "") for s in jobs["release-gates"]["steps"])


def test_publish_preflight_precedes_login_and_pins_source():
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8"))
    publish = workflow["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "read", "actions": "read"}
    steps = publish["steps"]
    preflight = next(i for i, s in enumerate(steps) if "verify_release.py preflight" in s.get("run", ""))
    assert "--require-ci" in steps[preflight]["run"] and "tag" in steps[preflight]["if"]
    assert preflight < next(i for i, s in enumerate(steps) if "docker/login-action" in s.get("uses", ""))
    assert steps[0]["with"]["fetch-depth"] == 0
    for step in steps:
        if step.get("id") in {"main", "runner"}:
            assert step["with"]["context"] == "${{ github.server_url }}/${{ github.repository }}.git#${{ github.sha }}"
            assert step["with"]["provenance"] == "mode=max"
            assert "org.opencontainers.image.revision=${{ github.sha }}" in step["with"]["labels"]
            assert "org.opencontainers.image.version=" in step["with"]["labels"]
            assert "org.opencontainers.image.source=" in step["with"]["labels"]
    release_runs = "\n".join(s.get("run", "") for s in workflow["jobs"]["release"]["steps"])
    assert "verify_release.py body" in release_runs
