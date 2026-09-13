# Phase 6 — Restricted plugin runtime

## Release gate

**OPEN — Docker hostile-runtime evidence is required before acceptance.** The
Windows development host used for this implementation has no reachable Docker
Engine, so static and unit evidence must not be represented as a security proof.

## Requirements and evidence

| Requirement | Implementation/evidence | Status |
| --- | --- | --- |
| FR-003 | `src/musicdl/contracts/plugin.py`, `src/musicdl/plugins/`; contract and client unit tests | Unit evidence complete; Docker pending |
| FR-010 | `src/musicdl_plugin_runner/`; `tests/integration/test_plugin_runtime.py` | Hostile Docker run pending |
| NFR-001 | `compose.yaml`, `docker/plugin/Dockerfile`, seccomp/Deno hosts; `tests/unit/test_compose_contract.py` | Static evidence complete; runtime pending |
| NFR-002 | Supervisor limits, bounded protocol, cleanup tests, hostile Docker suite | Unit evidence complete; runtime pending |

## Commands and results

- Baseline regression: `261 passed, 1 skipped` (exit 0; existing Redis skip because
  `MUSICDL_TEST_REDIS_URL` is unset).
- Task 8 integration command:
  `python -m pytest tests/integration/test_plugin_runtime.py -q -rs`.
  Docker availability/build result must be recorded by the accepting host. If
  `docker version` fails, the suite has the exact skip reason `Docker Engine is not available`.
- Required Docker evidence when available: `docker compose build --no-cache
  plugin-runner` (exit 0), `docker compose config` (exit 0), `docker compose up -d
  plugin-runner` (exit 0), integration suite (exit 0, zero skips), and `docker compose
  ps` showing a healthy runner.

## Attack coverage

The integration suite executes valid Python and JavaScript plugins and parameterizes
environment, `/proc`, shadow, Docker socket, Telegram session, main/public network,
subprocess/fork/command, CPU, memory, PID/tmp/output exhaustion, traversal, and
retained-file attempts. Every denial checks runner health and absence of the canary in
the response and runner logs.

## Limitations and rollback

Static tests cannot prove kernel seccomp, cgroup, Deno permission, or Docker network
isolation. Rollback is the scoped revert of the Task 8 acceptance commit; no runtime
credentials or external Redis secrets are introduced by the tests.
