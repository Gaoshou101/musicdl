# Phase 5 AI Assistance — Task 4 Summary

## Acceptance

Task 4 implements four-category language advice using the Phase 4 `normalize_language()` boundary. Provider input is limited to title, artist, and album; identifiers and download concerns are not disclosed. Invalid provider output and provider failures return deterministic, redacted caller fallbacks, with invalid caller fallbacks normalized to `未知`. Cancellation propagates and recorder failures are isolated.

FR-011 and FR-006 evidence: `advise_language()` validates strict output, emits stable `AIEvent` values, and exposes only `AILanguageResult`. OPS-001 evidence: `.env.example` documents disabled-by-default OpenAI-compatible settings; README states live-provider compatibility remains deployment validation.

## Verification evidence

- Required RED: `python -m pytest tests/unit/test_ai_language.py::test_valid_language_advice_is_applied -q -W error` — exit 1; collection failed because `advise_language` was absent from `musicdl.ai`.
- Language tests: `python -m pytest tests/unit/test_ai_language.py -q -W error` — exit 0; 26 passed.
- Focused Phase 5 matrix: `python -m pytest tests/unit/test_config.py tests/unit/test_ai_models.py tests/unit/test_ai_client.py tests/unit/test_ai_ranking.py tests/unit/test_ai_language.py tests/unit/test_source_search.py tests/unit/test_media_validation.py -q -W error` — exit 0; 132 passed.
- Full suite: `python -m pytest -q -W error` — exit 0; 297 passed, 1 skipped. The Redis integration test was skipped because `MUSICDL_TEST_REDIS_URL` is not configured.
- Dependency check: `python -m pip check` — exit 0; no broken requirements found.
- Whitespace check: `git diff --check` — exit 0.
- Final suite and diff checks after this summary were rerun as required and remained exit 0 (297 passed, 1 skipped; diff clean).

Live provider behavior was not exercised and is intentionally not claimed. Rollback is by reverting the later documentation/fix commit first if needed, then reverting implementation commit `1cc4885`; the final accepted Phase 5 range is recorded after acceptance. No media, data, or worktrees are involved.

## Changed files

- `src/musicdl/ai/service.py`
- `src/musicdl/ai/__init__.py`
- `tests/unit/test_ai_language.py`
- `.env.example`
- `README.md`
- `.planning/phases/05-ai-assistance/SUMMARY.md`
