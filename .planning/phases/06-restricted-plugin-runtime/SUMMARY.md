# Phase 6 — Restricted plugin runtime

## Release gate

**CLOSED — mandatory Docker hostile-runtime acceptance passed.** The suite ran
on Ubuntu 22.04 WSL (ext4) with Docker Engine 29.1.3 and Compose 2.40.3.

## Requirements and evidence

| Requirement | Implementation/evidence | Status |
| --- | --- | --- |
| FR-003 | `src/musicdl/contracts/plugin.py`, `src/musicdl/plugins/`; contract and client unit tests | Complete |
| FR-010 | `src/musicdl_plugin_runner/`; `tests/integration/test_plugin_runtime.py` | Complete; Docker hostile suite passed |
| NFR-001 | `compose.yaml`, `docker/plugin/Dockerfile`, seccomp/Deno hosts; `tests/unit/test_compose_contract.py` and Docker inspect assertions | Complete |
| NFR-002 | Supervisor limits, bounded protocol, cleanup tests, hostile Docker suite | Complete |

## Commands and results

- Docker image build: `docker compose build --no-cache plugin-runner` (exit 0).
- Compose validation/start: `docker compose config` and `docker compose up -d
  plugin-runner` (exit 0); `docker compose ps` reported a healthy runner.
- Docker hostile acceptance: `python3 -m pytest -c /dev/null -p
  no:cacheprovider tests/integration/test_plugin_runtime.py -q -rs` (exit 0;
  `37 passed, 0 skipped`, 630.45s). The distro pytest emitted three marker/config
  warnings because pytest 6.2.5 does not support this project's `pythonpath`
  option; the command explicitly disabled project config and imported from the
  container/runtime environment.
- Windows independent evidence: integration exit 0 (`37 skipped`, exact Docker
  unavailable reason); final full suite exit 0 (`377 passed, 70 skipped, 2 warnings`).

## Attack coverage

The integration suite executes valid Python and JavaScript plugins and parameterizes
environment, `/proc`, shadow, Docker socket, Telegram session, main/public network,
subprocess/fork/command, CPU, memory, PID/tmp/output exhaustion, traversal, and
retained-file attempts. Every denial checks runner health and absence of the canary in
the response and runner logs.

## Limitations and rollback

Static tests cannot prove kernel seccomp, cgroup, Deno permission, or Docker network
isolation; the Docker run above supplies the required runtime evidence. Rollback is
the scoped revert of the Phase 6 commits (or Task 8 acceptance commit); no runtime
credentials or external Redis secrets are introduced by the tests.
