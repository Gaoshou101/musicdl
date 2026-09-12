# Phase 0 Summary — Protocol and Reuse Foundation

## Status

Implemented and verified locally and on a remote Debian 13 Docker host on 2026-09-11.

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
remote Docker Engine 29.8.0 / Compose v5.5.1: config and no-cache build passed
remote Compose runtime: both services healthy
container identity: uid/gid 10001:10001 for both services
runtime hardening: read-only roots, cap_drop ALL and no-new-privileges observed
network boundary: plugin has only the internal control network, no host port and no external DNS/HTTP egress
secret boundary: plugin has no mounts and no Redis/Telegram/API/secret/token/password/session-shaped environment keys
storage boundary: only the main service mounts app, media and Telegram-session volumes
```

The first remote build exposed pip root and Debian system-user UID warnings. A TDD correction added failing Dockerfile contract assertions, then switched to explicit pip warning suppression and a fixed non-system nologin user. Commit `9a13d7f` passed the 27-test suite and a second no-cache remote build without those warnings.

## Commits

- `ac3b4a9` — dependency and Phase 0 planning baseline;
- `1752af9` — versioned configuration, redaction and plugin contracts;
- `e67d00a` — secure two-service scaffold and static deployment tests.
- `9a13d7f` — warning-free fixed-UID Docker user setup and regression tests.

All implementation commits contain SSH signatures and were pushed to `origin/codex/phase-0-foundation`.

## Deferred verification

- no real external Redis connection was attempted because Phase 0 used the documented invalid placeholder URL;
- no WeCom, Telegram, AI provider or third-party plugin connection was attempted;
- container resource limits and allowlisted plugin egress remain Phase 6 work.

These are explicit later-phase gates, not inferred successes. The temporary remote Compose stacks and their volumes, networks, images, source directories and archives were removed after validation.

## Next phase

Phase 1 begins with an official-protocol and real-application WeCom callback probe, followed by signature/decryption fixtures, idempotency and the minimal whitelist/search-command vertical slice.
