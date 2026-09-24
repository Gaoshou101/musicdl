"""The source-built main-image job must check the final wheel and panel artifacts."""

import ast
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_admin_panel_job_inspects_wheel_and_static_export_in_the_built_container():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    panel_job = data["jobs"]["admin-panel"]
    runs = [step.get("run", "") for step in panel_job["steps"]]
    combined = "\n".join(runs)

    assert sum("compose.prod.yaml build musicdl" in run for run in runs) == 1
    assert "compose up --detach --no-build musicdl" in combined
    assert "compose exec -T musicdl python -" in combined
    assert "from importlib.resources import files" in combined
    assert 'files("musicdl").joinpath("templates", "admin_dashboard.html").is_file()' in combined
    assert 'source_root = Path("/app/src").resolve()' in combined
    assert "assert not source_root.exists()" in combined
    assert "module_path.is_relative_to(source_root)" in combined
    assert 'panel = Path("/app/panel")' in combined
    assert '(panel / "index.html").is_file()' in combined
    assert 'panel / "_next" / "static"' in combined
    assert "static_assets.rglob(\"*\")" in combined

    runtime = next(run for run in runs if "compose exec -T musicdl python -" in run)
    python_body = runtime.split("compose exec -T musicdl python - <<'PY' || fail\n", 1)[1].split("\nPY\n", 1)[0]
    ast.parse(python_body)

    # Keep the existing user-facing, health, and auth-boundary probes in the
    # same source-built container smoke test.
    assert "/healthz" in combined
    assert "管理后台" in combined
    assert "/admin/sources" in combined
    assert ' = "401"' in combined
