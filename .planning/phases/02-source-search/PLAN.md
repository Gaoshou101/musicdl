# Phase 2: Source abstraction and deterministic search

## Scope and decisions

- Add a bounded Pydantic `Candidate`, a protocol-based source adapter and an explicit enabled/priority registry.
- Aggregate enabled sources with `asyncio` concurrency and independent per-source timeouts.
- Isolate malformed, mismatched, failed, and timed-out sources; never expose query, payload, or exception text in status fields.
- Normalize Unicode/whitespace, deduplicate matching cross-source versions by title/artist plus version-distinguishing metadata, select the preferred source deterministically, and hash the complete ordered public candidate representation for the candidate-set version.
- Format bounded WeCom results with source version, metadata, and IEC byte sizes.

## Acceptance and verification

Focused unit tests cover model bounds/normalization, registry duplicate and enable behavior, overlap/timeout/error/isolation, cross-source deduplication, query relevance, priority tie-breaking, deterministic ordering/version, and WeCom fields/size. Run the focused files followed by `& '.\.venv\Scripts\python.exe' -m pytest -q -W error`.

## Deferred integration

Wiring into the Phase 1 WeCom service/state and real Telegram or plugin sources is deferred to later phases; no live provider execution occurs here.

## Rollback

Remove only the Phase 2 files listed in the task allowlist. Preserve all pre-existing Phase 1 changes. Sol may use a later revert after acceptance.
