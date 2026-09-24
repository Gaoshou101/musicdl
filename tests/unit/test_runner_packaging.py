from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker/plugin/Dockerfile"
PACKAGE_CONFIG = ROOT / "docker/plugin/pyproject.toml"


def test_runner_package_declares_only_its_runtime_dependencies():
    project = tomllib.loads(PACKAGE_CONFIG.read_text(encoding="utf-8"))["project"]

    assert project["name"] == "musicdl-plugin-runner"
    assert project["version"] == "1.0.1"
    assert project["dependencies"] == [
        "fastapi==0.141.1",
        "pydantic==2.13.5",
        "uvicorn==0.52.4",
    ]


def test_runner_package_contains_only_runner_and_plugin_contracts():
    config = tomllib.loads(PACKAGE_CONFIG.read_text(encoding="utf-8"))

    assert config["tool"]["setuptools"]["packages"]["find"] == {
        "where": ["src"],
        "include": ["musicdl", "musicdl.contracts", "musicdl_plugin_runner"],
        "namespaces": False,
    }
    assert config["tool"]["setuptools"]["package-data"] == {
        "musicdl_plugin_runner": ["*.js"]
    }


def test_plugin_image_builds_and_installs_the_scoped_wheel():
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "AS runner-package" in text
    assert "COPY docker/plugin/pyproject.toml ./pyproject.toml" in text
    assert "COPY src/musicdl/__init__.py ./src/musicdl/__init__.py" in text
    assert "COPY src/musicdl/contracts/__init__.py ./src/musicdl/contracts/__init__.py" in text
    assert "COPY src/musicdl/contracts/plugin.py ./src/musicdl/contracts/plugin.py" in text
    assert "COPY src/musicdl_plugin_runner ./src/musicdl_plugin_runner" in text
    assert "pip wheel --no-cache-dir --no-deps --wheel-dir /wheels ." in text
    assert "COPY src ./src" not in text
    assert "COPY pyproject.toml README.md ./" not in text
    runner_wheel = "musicdl_plugin_runner-1.0.1-py3-none-any.whl"
    assert f"COPY --from=runner-package /wheels/{runner_wheel} /tmp/{runner_wheel}" in text
    assert f"/tmp/{runner_wheel}" in text
    assert "/tmp/musicdl-plugin-runner.whl" not in text


def test_all_runner_and_contract_sources_needed_at_runtime_are_present():
    required = (
        "src/musicdl/__init__.py",
        "src/musicdl/contracts/__init__.py",
        "src/musicdl/contracts/plugin.py",
        "src/musicdl_plugin_runner/__init__.py",
        "src/musicdl_plugin_runner/app.py",
        "src/musicdl_plugin_runner/deno_host.js",
        "src/musicdl_plugin_runner/lx_shim.js",
        "src/musicdl_plugin_runner/python_host.py",
        "src/musicdl_plugin_runner/seccomp.py",
        "src/musicdl_plugin_runner/supervisor.py",
    )

    assert all((ROOT / path).is_file() for path in required)
