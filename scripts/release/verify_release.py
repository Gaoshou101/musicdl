"""Read-only release consistency checks. Missing evidence is never PASS.

Buildx JSON shapes follow its official imagetools inspect reference and loader:
https://github.com/docker/buildx/blob/master/docs/reference/buildx_imagetools_inspect.md
https://github.com/docker/buildx/blob/master/util/imagetools/loader.go
The platform-to-attestation link is checked via index reference annotations;
Buildx exposes SLSA predicates, not raw in-toto statement subjects/signatures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PLATFORMS = ("linux/amd64", "linux/arm64")
IMAGES = ("wit7zz/musicdl", "wit7zz/musicdl-plugin-runner")
REPO = "Gaoshou101/musicdl"
METADATA_FILES = (
    "pyproject.toml", "docker/plugin/pyproject.toml", "web/package.json", "web/package-lock.json",
    "docker/main/Dockerfile", "docker/plugin/Dockerfile", ".env.example", "compose.quick.yaml",
    "compose.prod.yaml", "README.md", "README.zh-CN.md",
)


class Unavailable(RuntimeError):
    pass


def command(args, *, root=ROOT):
    try:
        result = subprocess.run(args, cwd=root, capture_output=True, text=True,
                                encoding="utf-8", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable(f"{args[0]} unavailable or timed out") from exc
    if result.returncode:
        # Do not repeat CLI stderr: it can contain authentication configuration.
        raise Unavailable(f"{args[0]} command failed (exit {result.returncode})")
    return result.stdout.rstrip("\r\n")


def json_command(args, run):
    try:
        return json.loads(run(args))
    except (json.JSONDecodeError, TypeError) as exc:
        raise Unavailable(f"{args[0]} returned malformed JSON") from exc


class Report:
    def __init__(self, mode, tag=None):
        self.mode, self.tag, self.source_commit = mode, tag, None
        self.checks = []

    def check(self, key, actual, expected, *, detail="", ok=None):
        self.checks.append({"id": key, "status": "PASS" if (actual == expected if ok is None else ok) else "FAIL",
                            "expected": expected, "actual": actual, "detail": detail})

    def unavailable(self, key, detail):
        self.checks.append({"id": key, "status": "NOT_RUN", "expected": "verifiable evidence",
                            "actual": None, "detail": detail})

    def as_dict(self):
        statuses = {item["status"] for item in self.checks}
        status = "FAIL" if "FAIL" in statuses else "NOT_RUN" if "NOT_RUN" in statuses else "PASS"
        return {"schema_version": 1, "mode": self.mode, "tag": self.tag,
                "source_commit": self.source_commit, "status": status, "checks": self.checks}

    def exit_code(self):
        return {"PASS": 0, "FAIL": 1, "NOT_RUN": 2}[self.as_dict()["status"]]


def normalized(text):
    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def release_body(notes, tag, commit, main_digest, runner_digest):
    if not tag.startswith("v") or not VERSION.fullmatch(tag[1:]):
        raise ValueError("invalid release tag")
    if not COMMIT.fullmatch(commit) or not all(DIGEST.fullmatch(d) for d in (main_digest, runner_digest)):
        raise ValueError("invalid source commit or image digest")
    return normalized(notes) + (
        f"\n## Source / 源码\n\nSource commit: `{commit}`\n"
        "\n## Docker images / Docker 镜像\n\n"
        f"- `{IMAGES[0]}:{tag[1:]}` — `{main_digest}`\n"
        f"- `{IMAGES[1]}:{tag[1:]}` — `{runner_digest}`\n"
        "\nBoth OCI image indexes were built for `linux/amd64` and `linux/arm64` by this workflow. / "
        "此工作流为 `linux/amd64` 和 `linux/arm64` 构建了两个 OCI 镜像索引。\n")


def preflight(root=ROOT, tag=None, run=None):
    run = run or (lambda args: command(args, root=root))
    report = Report("preflight", tag)
    try:
        version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        report.check("version-format", version, "MAJOR.MINOR.PATCH", ok=isinstance(version, str) and bool(VERSION.fullmatch(version)))
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            return report
    except (OSError, ValueError, KeyError, TypeError):
        report.check("version-format", "missing or invalid pyproject.toml", "MAJOR.MINOR.PATCH")
        return report

    def field(key, read, expected=version):
        try:
            report.check(key, read(), expected)
        except (OSError, ValueError, KeyError, TypeError):
            report.check(key, "missing or malformed", expected)

    field("plugin-version", lambda: tomllib.loads((root / "docker/plugin/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    field("web-version", lambda: json.loads((root / "web/package.json").read_text(encoding="utf-8"))["version"])
    field("web-lock-version", lambda: json.loads((root / "web/package-lock.json").read_text(encoding="utf-8"))["version"])
    field("web-lock-root-version", lambda: json.loads((root / "web/package-lock.json").read_text(encoding="utf-8"))["packages"][""]["version"])
    for role, wheel in (("main", "musicdl"), ("plugin", "musicdl_plugin_runner")):
        field(f"{role}-wheel", lambda role=role, wheel=wheel: sorted(set(re.findall(
            rf"\b{wheel}-([^/\s]+)-py3-none-any\.whl\b", (root / f"docker/{role}/Dockerfile").read_text(encoding="utf-8")))), [version])
    field("env-image-version", lambda: re.findall(r"(?m)^MUSICDL_IMAGE_TAG=([^\s#]+)\s*$", (root / ".env.example").read_text(encoding="utf-8")), [version])
    try:
        import yaml  # Existing dev dependency, also installed in the publish job.
        quick = yaml.safe_load((root / "compose.quick.yaml").read_text(encoding="utf-8"))
        prod = yaml.safe_load((root / "compose.prod.yaml").read_text(encoding="utf-8"))
        for service, image in zip(("musicdl", "plugin-runner"), IMAGES):
            report.check(f"quick-{service}", quick["services"][service]["image"], f"{image}:${{MUSICDL_IMAGE_TAG:-{version}}}")
            report.check(f"prod-{service}", prod["services"][service]["image"], f"{image}:${{MUSICDL_IMAGE_TAG:-latest}}")
    except ImportError:
        report.unavailable("compose-images", "PyYAML dev dependency is unavailable")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        report.check("compose-images", "missing or malformed", "two configured versioned application images")
    for filename in ("README.md", "README.zh-CN.md"):
        field(f"{filename}-image-table", lambda filename=filename: sorted(re.findall(
            r"(?m)^\| `(wit7zz/musicdl(?:-plugin-runner)?:[^`]+)` \|", (root / filename).read_text(encoding="utf-8"))),
              sorted(f"{image}:{version}" for image in IMAGES))
        # Deployment matrix is the current recommendation; historical examples
        # elsewhere in the README may deliberately use an older version.
        field(f"{filename}-recommended-version", lambda filename=filename: re.findall(
            r"(?m)^\|[^\n]*`compose\.prod\.yaml`[^\n]*MUSICDL_IMAGE_TAG=(\d+\.\d+\.\d+)\b",
            (root / filename).read_text(encoding="utf-8")), [version])
    notes_path = f"docs/release-notes/v{version}.md"
    notes = None
    try:
        notes = (root / notes_path).read_text(encoding="utf-8")
        sections = re.split(r"(?m)^## (English|中文)\s*$", notes)
        shape = (notes.splitlines()[0] == f"# musicdl {version}" and len(sections) == 5
                 and sections[1] == "English" and sections[3] == "中文"
                 and all(re.search(r"(?m)^- \S", section) for section in (sections[2], sections[4])))
        report.check("release-notes", shape and "Source commit:" not in notes and "## Docker images" not in notes,
                     True, detail=notes_path)
    except (OSError, IndexError):
        report.check("release-notes", "missing or empty", notes_path)
    try:
        report.source_commit = run(["git", "rev-parse", "HEAD"])
        report.check("source-commit", bool(COMMIT.fullmatch(report.source_commit)), True)
        if tag is not None:
            report.check("tag-version", tag, f"v{version}")
            if tag != f"v{version}":
                return report
            # A missing local tag is a definite failure, not a network skip.
            refs = run(["git", "for-each-ref", "--format=%(refname)", "refs/tags/"])
            present = f"refs/tags/{tag}" in refs.splitlines()
            report.check("tag-exists", present, True)
            if present:
                report.check("annotated-tag", run(["git", "cat-file", "-t", f"refs/tags/{tag}"]), "tag")
                report.check("tag-commit", run(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"]), report.source_commit)
                # Local fixes must not mask a mismatch in the tagged source.
                # HEAD diff includes both staged and unstaged tracked changes.
                report.check("committed-metadata", run(["git", "diff", "--name-only", "HEAD", "--", *METADATA_FILES]), "")
                if notes is not None:
                    report.check("committed-notes", normalized(run(["git", "show", f"HEAD:{notes_path}"])), normalized(notes))
    except Unavailable as exc:
        report.unavailable("git-evidence", str(exc))
    return report


def check_run(report, key, runs, commit, *, workflow, event, branch):
    if not isinstance(runs, list) or not all(isinstance(item, dict) for item in runs):
        report.check(key, "malformed evidence", "workflow run array")
        return
    candidates = [item for item in runs if item.get("headSha") == commit and item.get("event") == event
                  and item.get("headBranch") == branch and item.get("workflowName") == workflow]
    if any(not isinstance(item.get("createdAt"), str) or not isinstance(item.get("databaseId"), int) for item in candidates):
        report.check(key, "malformed ordering evidence", "run timestamp and numeric ID")
        return
    latest = max(candidates, key=lambda item: (item.get("createdAt", ""), item.get("databaseId", 0)), default=None)
    report.check(key, None if latest is None else {"status": latest.get("status"), "conclusion": latest.get("conclusion")},
                 {"status": "completed", "conclusion": "success"}, detail="latest matching source execution")


def ci_evidence(commit, repo, run):
    return json_command(["gh", "run", "list", "--repo", repo, "--workflow", "ci.yml", "--commit", commit,
                         "--event", "push", "--branch", "main", "--limit", "100", "--json",
                         "databaseId,workflowName,headSha,headBranch,status,conclusion,event,createdAt"], run)


def require_ci(report, repo, run):
    if not report.source_commit:
        report.unavailable("source-ci", "source commit is unavailable")
        return
    try:
        runs = ci_evidence(report.source_commit, repo, run)
        check_run(report, "source-ci", runs, report.source_commit, workflow="CI", event="push", branch="main")
    except (Unavailable, TypeError, AttributeError) as exc:
        report.unavailable("source-ci", str(exc) if isinstance(exc, Unavailable) else "unexpected CI evidence shape")


def git_source(source, commit, repo):
    """Only the primary Git input counts, not an arbitrary matching string."""
    if not isinstance(source, dict):
        return False
    uri = source.get("uri", "")
    if not isinstance(uri, str):
        return False
    try:
        parsed = urlsplit(uri.removeprefix("git+"))
    except ValueError:
        return False
    digest = source.get("digest")
    return (parsed.scheme == "https" and parsed.netloc == "github.com"
            and parsed.path.removesuffix(".git") == f"/{repo}"
            and not parsed.query and parsed.fragment in ("", commit)
            and isinstance(digest, dict) and digest.get("sha1") == commit)


def git_provenance(predicate, commit, repo):
    """Validate the primary input and dependency at explicit SLSA schema paths."""
    if not isinstance(predicate, dict):
        return False
    if "buildDefinition" in predicate or "runDetails" in predicate:
        definition = predicate.get("buildDefinition")
        if not isinstance(definition, dict):
            return False
        parameters = definition.get("externalParameters")
        if not isinstance(parameters, dict):
            return False
        source = parameters.get("configSource")
        dependencies = definition.get("resolvedDependencies")
    else:
        invocation = predicate.get("invocation")
        if not isinstance(invocation, dict):
            return False
        source = invocation.get("configSource")
        dependencies = predicate.get("materials")
    return (git_source(source, commit, repo) and isinstance(dependencies, list)
            and any(git_source(item, commit, repo) for item in dependencies))


def check_image(report, image, evidence, version, commit, repo):
    manifest, configs, provenance = evidence["manifest"], evidence["configs"], evidence["provenance"]
    digest = manifest.get("digest", "")
    report.check(f"{image}-index-digest", bool(DIGEST.fullmatch(digest)), True)
    report.check(f"{image}-index-type", manifest.get("mediaType") in (
        "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"), True)
    descriptors = manifest.get("manifests", [])
    for platform in PLATFORMS:
        os_name, arch = platform.split("/")
        matches = [item for item in descriptors if item.get("platform", {}).get("os") == os_name
                   and item.get("platform", {}).get("architecture") == arch]
        report.check(f"{image}-{platform}-manifest", len(matches), 1)
        if len(matches) != 1:
            continue
        platform_digest = matches[0].get("digest", "")
        references = [item for item in descriptors if
                      item.get("annotations", {}).get("vnd.docker.reference.digest",
                          item.get("annotations", {}).get("com.docker.reference.digest")) == platform_digest
                      and item.get("annotations", {}).get("vnd.docker.reference.type",
                          item.get("annotations", {}).get("com.docker.reference.type")) == "attestation-manifest"]
        report.check(f"{image}-{platform}-attestation-link", bool(DIGEST.fullmatch(platform_digest)) and bool(references), True)
        config = configs.get(platform, {})
        report.check(f"{image}-{platform}-config-platform", [config.get("os"), config.get("architecture")], [os_name, arch])
        labels = config.get("config", {}).get("Labels", {})
        for name, expected in (("revision", commit), ("version", version), ("source", f"https://github.com/{repo}")):
            report.check(f"{image}-{platform}-{name}", labels.get(f"org.opencontainers.image.{name}"), expected)
        predicate = provenance.get(platform, {}).get("SLSA", {})
        report.check(f"{image}-{platform}-git-provenance",
                     git_provenance(predicate, commit, repo), True,
                     detail="Buildx SLSA v0.2/v1 primary Git input and matching source dependency")
    return digest


def _check_published(report, evidence, notes, tag, repo):
    commit, version = report.source_commit, tag[1:]
    release = evidence["release"]
    if not isinstance(release, dict) or any(type(release.get(name)) is not bool for name in ("isDraft", "isPrerelease")):
        raise ValueError("invalid release state")
    report.check("release-state", {name: release.get(name) for name in ("tagName", "name", "isDraft", "isPrerelease")},
                 {"tagName": tag, "name": f"musicdl {version}", "isDraft": False, "isPrerelease": False})
    digests = [check_image(report, image, evidence["images"][image], version, commit, repo) for image in IMAGES]
    if all(isinstance(d, str) and DIGEST.fullmatch(d) for d in digests):
        expected = release_body(notes, tag, commit, *digests)
        actual = release.get("body", "")
        report.check("release-body", hashlib.sha256(normalized(actual).encode()).hexdigest(),
                     hashlib.sha256(normalized(expected).encode()).hexdigest(), detail="committed notes + source commit + registry digests")
    else:
        report.unavailable("release-body", "valid registry digests unavailable")
    check_run(report, "source-ci", evidence["ci"], commit, workflow="CI", event="push", branch="main")
    check_run(report, "publish-workflow", evidence["publish"], commit, workflow="Publish Docker images", event="push", branch=tag)


def check_published(report, evidence, notes, tag, repo=REPO):
    try:
        _check_published(report, evidence, notes, tag, repo)
    except (KeyError, TypeError, AttributeError, ValueError):
        report.check("published-evidence", "malformed evidence", "recognized GitHub and Buildx JSON")


def collect_published(tag, commit, repo, run):
    evidence = {"images": {}}
    evidence["release"] = json_command(["gh", "release", "view", tag, "--repo", repo,
                                       "--json", "tagName,name,body,isDraft,isPrerelease"], run)
    evidence["ci"] = ci_evidence(commit, repo, run)
    evidence["publish"] = json_command(["gh", "run", "list", "--repo", repo, "--workflow", "docker-publish.yml",
                                       "--commit", commit, "--event", "push", "--limit", "100", "--json",
                                       "databaseId,workflowName,headSha,headBranch,status,conclusion,event,createdAt"], run)
    for image in IMAGES:
        ref = f"{image}:{tag[1:]}"
        manifest = json_command(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"], run)
        digest = manifest.get("digest", "")
        if not DIGEST.fullmatch(digest):
            raise Unavailable("Buildx did not return an index digest")
        # Pin subsequent reads to the measured digest, not the moving tag.
        pinned = f"{image}@{digest}"
        evidence["images"][image] = {"manifest": manifest,
            "configs": json_command(["docker", "buildx", "imagetools", "inspect", pinned, "--format", "{{json .Image}}"], run),
            "provenance": json_command(["docker", "buildx", "imagetools", "inspect", pinned, "--format", "{{json .Provenance}}"], run)}
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "published", "body"))
    parser.add_argument("--tag")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--source-root", type=Path, default=ROOT,
                        help="Git checkout to verify (defaults to the checker repository)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-ci", action="store_true")
    parser.add_argument("--source-commit")
    parser.add_argument("--main-digest")
    parser.add_argument("--runner-digest")
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    if not source_root.is_dir():
        parser.error("--source-root must be an existing directory")
    run = lambda command_args: command(command_args, root=source_root)
    if args.mode in {"published", "body"} and not args.tag:
        parser.error("--tag is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("invalid repository")
    if args.mode == "body":
        try:
            if not args.tag.startswith("v") or not VERSION.fullmatch(args.tag[1:]):
                raise ValueError("invalid release tag")
            notes = (source_root / f"docs/release-notes/{args.tag}.md").read_text(encoding="utf-8")
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(release_body(notes, args.tag, args.source_commit or "", args.main_digest or "", args.runner_digest or ""), end="")
            return 0
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    report = preflight(root=source_root, tag=args.tag, run=run)
    if args.require_ci:
        require_ci(report, args.repo, run)
    if args.mode == "published":
        report.mode = "published"
        if report.exit_code() == 0:
            try:
                evidence = collect_published(args.tag, report.source_commit, args.repo, run)
                notes = run(["git", "show", f"HEAD:docs/release-notes/{args.tag}.md"])
                check_published(report, evidence, notes, args.tag, args.repo)
            except (Unavailable, OSError) as exc:
                report.unavailable("published-evidence", str(exc))
            except (KeyError, TypeError, AttributeError, ValueError):
                report.check("published-evidence", "malformed evidence", "recognized GitHub and Buildx JSON")
    data = report.as_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=True, indent=2))
    else:
        for item in data["checks"]:
            print(f"[{item['id']}] {item['status']} {item['detail']}")
        print(f"{data['mode']}: {data['status']}")
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
