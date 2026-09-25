"""The quick-install CI smoke must exercise the published three-service stack."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
QUICK_COMPOSE = ROOT / "compose.quick.yaml"


def test_ci_smokes_the_published_quick_install_stack_in_an_isolated_project():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    smoke = data["jobs"]["quick-install-stack"]
    run = "\n".join(step.get("run", "") for step in smoke["steps"])

    assert smoke["runs-on"] == "ubuntu-latest"
    assert smoke["env"] == {"MUSICDL_PORT": "3998"}
    assert 'project="musicdl-quick-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in run
    assert 'docker compose --project-name "$project" --file compose.quick.yaml "$@"' in run
    assert "unset MUSICDL_IMAGE_TAG" in run
    assert "compose pull" in run
    assert "compose up --detach --wait --wait-timeout 120" in run
    assert "compose down --volumes --remove-orphans --timeout 20" in run
    assert "compose ps --all" in run and "compose logs --no-color --tail=100" in run

    # Test through the main app container so service DNS and app-network access
    # are covered, not just Redis's standalone container healthcheck.
    assert "compose exec -T musicdl python -" in run
    assert "Redis.from_url" in run and "client.ping()" in run
    assert "client.set(key, b\"ok\", ex=60)" in run
    assert "client.get(key) == b\"ok\"" in run

    assert "/healthz" in run and "${MUSICDL_PORT}" in run
    assert "quick-install-index.html" in run and "管理后台" in run
    assert "/admin/sources" in run and 'test "$status" = "401"' in run
    assert "secrets." not in str(smoke)
    assert "docker compose config" not in run
    assert "docker system prune" not in run

    quick_compose = yaml.safe_load(QUICK_COMPOSE.read_text(encoding="utf-8"))
    services = quick_compose["services"]
    assert services["musicdl"]["image"] == "wit7zz/musicdl:${MUSICDL_IMAGE_TAG:-1.0.2}"
    assert services["plugin-runner"]["image"] == "wit7zz/musicdl-plugin-runner:${MUSICDL_IMAGE_TAG:-1.0.2}"
