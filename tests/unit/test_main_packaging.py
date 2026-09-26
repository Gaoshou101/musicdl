from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WHEEL_NAME = "musicdl-1.0.3-py3-none-any.whl"


def test_installed_main_wheel_contains_importable_application_and_static_assets(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    wheel = wheelhouse / WHEEL_NAME
    assert wheel.is_file(), built.stdout + built.stderr
    install_root = tmp_path / "installed"
    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--target",
            str(install_root),
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    script = r'''
import sys
from importlib.resources import files
from pathlib import Path

import musicdl
import musicdl.admin.portal as portal
import musicdl.app
import musicdl_plugin_runner
import musicdl_plugin_runner.supervisor

installed_root = Path(sys.argv[1]).resolve()
for module in (musicdl, portal, musicdl.app, musicdl_plugin_runner, musicdl_plugin_runner.supervisor):
    module_path = Path(module.__file__).resolve()
    assert module_path.is_relative_to(installed_root), (module.__name__, module_path)

template = files("musicdl").joinpath("templates", "admin_dashboard.html")
assert template.is_file()
assert "<h1>musicdl 管理后台</h1>" in template.read_text(encoding="utf-8")
assert "<h1>musicdl 管理后台</h1>" in portal.render_dashboard()

deno_host = files("musicdl_plugin_runner").joinpath("deno_host.js")
lx_shim = files("musicdl_plugin_runner").joinpath("lx_shim.js")
assert deno_host.is_file() and "invocation.request" in deno_host.read_text(encoding="utf-8")
assert lx_shim.is_file() and "lx-music custom-source adapter" in lx_shim.read_text(encoding="utf-8")
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(install_root)
    environment.pop("PYTHONHOME", None)
    checked = subprocess.run(
        [sys.executable, "-c", script, str(install_root)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
