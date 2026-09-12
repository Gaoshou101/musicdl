# Phase 3 Summary — Telegram User-Account Connector Gate

## Status

Phase 3 code and controlled tests are complete. Live Telegram account, selected public/custom Bot behavior, proxy path, and service limits remain deployment-environment validation gates and are not claimed as observed here.

## Delivered

- Optional Telegram configuration with positive API ID validation, secret API hash handling, restricted profile names, and a dedicated session root.
- Lazy Telethon 1.45.0 client construction with code login, 2FA completion, restart restoration, invalid-session state, and observable flood-wait retry seconds.
- Authorization re-check after code/password sign-in so incomplete authentication cannot be reported as ready.
- Restricted session paths and permission hardening for the session database and SQLite WAL/SHM artifacts.
- An authorized Telethon conversation bridge that sends one Bot command and delegates raw-response decoding to the selected Bot contract.
- `PublicTelegramBot` with the built-in `/search {query}` contract and `CustomTelegramBot` with a validated single-placeholder command template.
- Bounded Bot queries and response counts, safe failure messages, normalized media records, and mapping into the Phase 2 `Candidate` model.
- Compose isolation retained: only the main service mounts `musicdl-telegram`; `plugin-runner` receives no session volume.

## Verification evidence

- TDD RED evidence was recorded for the new Bot API, empty-result correction, incomplete authorization checks, and connector Bot requester bridge.
- Focused Phase 3/source/Compose suite: 73 passed with warnings treated as errors.
- Full suite: 142 passed, 1 skipped with warnings treated as errors.
- The sole skip is the environment-gated WeCom Redis integration test because `MUSICDL_TEST_REDIS_URL` is not configured.
- Telethon import/version check: 1.45.0.
- `pip check`: no broken requirements.
- `git diff --check`: passed; only Git line-ending conversion notices were emitted.

## Boundaries

- No real Telegram credentials, session contents, or Bot responses were used or logged.
- Public and custom Bot response formats remain explicit decoder contracts because external Bot payloads are not stable or uniform.
- Phase 4 owns media download, content/type/size validation, path safety, and fallback behavior.

## Rollback

Revert the Phase 3 commit rather than deleting session data or rewriting published history. Never force-push, and back up a real Telegram session volume before any deployment rollback that changes its storage lifecycle.
