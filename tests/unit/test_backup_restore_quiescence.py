"""Backup and restore must quiesce every Compose volume before writing data."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "backup_restore.ps1"
LOGICAL_VOLUMES = (
    "musicdl-media",
    "musicdl-app-data",
    "musicdl-telegram",
    "musicdl-redis-data",
)


def powershell() -> str:
    host = shutil.which("pwsh") or shutil.which("powershell")
    if not host:
        pytest.skip("PowerShell is required to exercise the backup script")
    return host


def fake_docker(tmp_path: Path, *, active: bool = False, query_failure: bool = False) -> tuple[dict[str, str], Path]:
    log = tmp_path / "docker-calls.txt"
    stub = tmp_path / "docker_stub.py"
    stub.write_text(
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['MUSICDL_DOCKER_LOG'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(args) + '\\n')\n"
        "if args and args[0] == 'ps':\n"
        "    if os.environ.get('MUSICDL_DOCKER_QUERY_FAILURE') == 'true':\n"
        "        raise SystemExit(9)\n"
        "    if os.environ.get('MUSICDL_DOCKER_ACTIVE') == 'true':\n"
        "        print('container-id')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        docker = tmp_path / "docker.cmd"
        docker.write_text(
            "@echo off\r\n"
            "python \"%MUSICDL_DOCKER_STUB%\" %*\r\n"
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
    else:
        docker = tmp_path / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "exec python3 \"$MUSICDL_DOCKER_STUB\" \"$@\"\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    env["MUSICDL_DOCKER_LOG"] = str(log)
    env["MUSICDL_DOCKER_STUB"] = str(stub)
    env["MUSICDL_DOCKER_ACTIVE"] = "true" if active else "false"
    env["MUSICDL_DOCKER_QUERY_FAILURE"] = "true" if query_failure else "false"
    return env, log


def invoke(tmp_path: Path, action: str, *, active: bool = False, query_failure: bool = False):
    target = tmp_path / "archives"
    target.mkdir(exist_ok=True)
    if action == "restore":
        for logical in LOGICAL_VOLUMES:
            (target / f"{logical}.tar.gz").write_bytes(b"archive")
    env, log = fake_docker(tmp_path, active=active, query_failure=query_failure)
    result = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
         "-Action", action, "-Target", str(target), "-ConfirmIsolated"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_refuses_a_running_container_using_a_volume_before_archive_or_restore(action, tmp_path):
    result, log = invoke(tmp_path, action, active=True)

    assert result.returncode == 2
    assert "running container" in result.stdout
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 4
    assert all(call.startswith("ps ") for call in calls)
    assert [f"volume=musicdl_{logical}" in call for call, logical in zip(calls, LOGICAL_VOLUMES)] == [True] * 4


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_query_failure_stops_before_archive_or_restore(action, tmp_path):
    result, log = invoke(tmp_path, action, query_failure=True)

    assert result.returncode == 3
    assert "could not verify" in result.stdout
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1 and calls[0].startswith("ps ")


def test_checks_all_four_volumes_before_first_backup_archive(tmp_path):
    result, log = invoke(tmp_path, "backup")

    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) >= 5
    assert all(call.startswith("ps ") for call in calls[:4])
    assert [f"volume=musicdl_{logical}" in calls[index] for index, logical in enumerate(LOGICAL_VOLUMES)] == [True] * 4
    assert calls[4].startswith("run ")
    assert "tar czf" in calls[4]
    assert result.returncode == 1
