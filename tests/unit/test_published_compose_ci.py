"""The published Compose smoke exercises the user-facing default image pair."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
COMPOSE = ROOT / "compose.yaml"


def test_ci_smokes_the_unified_published_stack_with_bundled_redis():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    smoke = data["jobs"]["quick-install-stack"]
    run = "\n".join(step.get("run", "") for step in smoke["steps"])

    assert smoke["runs-on"] == "ubuntu-latest"
    assert smoke["env"] == {"MUSICDL_PORT": "3998", "MUSICDL_IMAGE_TAG": "1.2.0"}
    assert 'project="musicdl-quick-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in run
    assert 'docker compose --project-name "$project" --file compose.yaml "$@"' in run
    assert "MUSICDL_IMAGE_TAG" not in run
    assert "compose pull" in run
    assert "compose up --detach --no-build --pull never" in run or "compose up --detach --no-build --pull missing" in run
    assert "compose down --volumes --remove-orphans --timeout 20" in run
    assert "compose ps --all" in run and "compose logs --no-color --tail=100" in run

    assert "compose exec -T musicdl python -" in run
    assert "Redis.from_url" in run and "client.ping()" in run
    assert "client.set(key, b\"ok\", ex=60)" in run
    assert "client.get(key) == b\"ok\"" in run
    assert "/healthz" in run and "${MUSICDL_PORT}" in run
    assert "quick-install-index.html" in run and "管理后台" in run
    assert "/admin/sources" in run and 'test "$status" = "401"' in run
    assert "secrets." not in str(smoke)
    assert "docker system prune" not in run

    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert compose["services"]["musicdl"]["image"] == "wit7zz/musicdl:${MUSICDL_IMAGE_TAG:-1.2.0}"
    assert compose["services"]["plugin-runner"]["image"] == "wit7zz/musicdl-plugin-runner:${MUSICDL_IMAGE_TAG:-1.2.0}"
    assert compose["services"]["redis"]["image"] == "redis:8.8.3-alpine"
