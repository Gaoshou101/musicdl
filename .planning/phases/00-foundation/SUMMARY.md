# Phase 0 Summary — Protocol and Reuse Foundation

## Status

Implemented and locally verified on 2026-09-11. Docker image build, Compose interpolation and container-runtime isolation remain pending because Docker is not available on the current host.

## Delivered

- Python 3.12 project metadata with exact direct dependency versions and license/source evidence;
- `AppSettings` configuration schema version `1`, external Redis URL protection, explicit media/session separation and internal plugin URL validation;
- recursive masking for passwords, secrets, tokens, API keys, authorization and session-shaped fields;
- correlation-safe `musicdl.plugin/v1` request/response envelopes, JSON/size limits and artifact path validation;
- minimal FastAPI health endpoints for `musicdl` and `plugin-runner`;
- two-service Compose file with no Redis service, loopback-only main port, non-root users, read-only root filesystems, dropped capabilities, `no-new-privileges` and `/tmp` tmpfs;
- internal-only plugin control network with no plugin Redis/session/secret mounts;
- pinned Python Dockerfiles, placeholder-only environment example and operational README.

## TDD evidence

Three RED checkpoints were observed before production corrections:

1. configuration/contracts: missing `musicdl` implementation caused collection failure;
2. protocol correction: missing correlation/artifact models caused collection failure;
3. services/Compose: missing apps and deployment files caused eight expected failures.

Subsequent security corrections also produced expected failing tests for path overlap/schemes, Compose plugin routing/internal network, and Docker group creation before reaching GREEN.

Final independent verification:

```text
pytest -q -W error: 27 passed, exit 0, no warnings
pip check: no broken requirements, exit 0
editable install: musicdl and musicdl_plugin_runner import successfully
main live probe: {"service":"musicdl","status":"ok","config_version":1}
plugin live probe: {"service":"plugin-runner","status":"ok","protocol":"musicdl.plugin/v1"}
Compose YAML/static contract: passed
secret-shape scan: clean
temporary Uvicorn listeners after cleanup: none
```

## Commits

- `ac3b4a9` — dependency and Phase 0 planning baseline;
- `1752af9` — versioned configuration, redaction and plugin contracts;
- `e67d00a` — secure two-service scaffold and static deployment tests.

All three commits contain SSH signatures and were pushed to `origin/codex/phase-0-foundation`.

## Deferred verification

- Docker images were not built;
- `docker compose config/up/ps` was not run;
- container UID/GID, read-only filesystem, capability dropping and internal network isolation were not observed at runtime;
- no Redis, WeCom, Telegram, AI or external plugin connection was attempted.

These are explicit deployment/next-phase gates, not inferred successes.

## Next phase

Phase 1 begins with an official-protocol and real-application WeCom callback probe, followed by signature/decryption fixtures, idempotency and the minimal whitelist/search-command vertical slice.
