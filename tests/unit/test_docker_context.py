from __future__ import annotations

import fnmatch
import glob
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _is_ignored(path: Path, patterns: list[str]) -> bool:
    """Match the simple file and directory patterns used by this .dockerignore."""
    relative = path.relative_to(ROOT).as_posix()
    parts = relative.split("/")
    prefixes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]

    for pattern in patterns:
        pattern = pattern.strip("/")
        if "/" not in pattern:
            if any("/" not in prefix and fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes):
                return True
        elif any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes):
            return True
    return False


def _local_copy_sources(dockerfile: Path) -> list[Path]:
    sources: list[Path] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        operands = shlex.split(stripped[5:])
        if any(operand.startswith("--from=") for operand in operands):
            continue
        operands = [operand for operand in operands if not operand.startswith("--")]
        for source in operands[:-1]:
            matches = glob.glob(str(ROOT / source))
            assert matches, f"COPY source {source!r} in {dockerfile.relative_to(ROOT)} does not exist"
            sources.extend(Path(match) for match in matches)
    return sources


def test_context_excludes_local_state_and_user_runtime_files():
    patterns = _dockerignore_patterns()
    for expected in (
        ".deno-cache",
        ".worktrees",
        ".planning",
        ".pytest-tmp*",
        "**/__pycache__",
        "**/*.py[cod]",
        ".agents",
        ".codex",
        ".superpowers",
        "data",
        "plugins/uploads",
        "*.session",
        "*.session-journal",
    ):
        assert expected in patterns


def test_dockerfile_copy_inputs_remain_in_the_build_context():
    patterns = _dockerignore_patterns()
    dockerfiles = (ROOT / "docker/main/Dockerfile", ROOT / "docker/plugin/Dockerfile")
    sources = [source for dockerfile in dockerfiles for source in _local_copy_sources(dockerfile)]

    assert {source.relative_to(ROOT).as_posix() for source in sources} >= {
        "pyproject.toml",
        "README.md",
        "src",
        "web/package.json",
        "web/package-lock.json",
        "web/next.config.mjs",
        "web/postcss.config.mjs",
        "web/tsconfig.json",
        "web/next-env.d.ts",
        "web/src",
    }
    for source in sources:
        assert not _is_ignored(source, patterns), f"Docker COPY source is ignored: {source.relative_to(ROOT)}"

        if source.is_dir() and source.name == "src":
            suffixes = {".py", ".js", ".ts", ".tsx", ".css"}
            for nested in source.rglob("*"):
                if nested.is_file() and nested.suffix in suffixes:
                    assert not _is_ignored(nested, patterns), (
                        f"Docker source file is ignored: {nested.relative_to(ROOT)}"
                    )

    assert _is_ignored(ROOT / "src/musicdl/__pycache__/app.cpython-312.pyc", patterns)
    assert not _is_ignored(ROOT / "src/musicdl/media/download.py", patterns)


def test_plugin_dockerfiles_and_future_plugin_packaging_files_are_not_ignored():
    patterns = _dockerignore_patterns()
    for relative in ("docker/plugin/Dockerfile", "docker/plugin/package.json"):
        assert not _is_ignored(ROOT / relative, patterns)
