# Phase 4 — Download, Media Safety and Fallback Summary

## Status

Implementation, Sol review, and automated verification accepted on 2026-09-12.

## Requirements and evidence

| Requirement | Delivered behavior | Exact implementation and test evidence |
|---|---|---|
| FR-005 | Confirmed candidates stream into the configured media root; paths use `<language>/<artist>/<title> - <artist>.<ext>`, sanitize unsafe components, and resolve deterministic collision suffixes without overwriting. | `src/musicdl/media/validation.py`: `sanitize_component`, `validated_destination`; `src/musicdl/media/download.py`: `download_candidate`; `tests/unit/test_media_validation.py`, `tests/unit/test_media_download.py` |
| FR-006 | Language values normalize to exactly `华语`, `欧美`, `日韩`, or `未知`; unrecognized values fall back to `未知`. | `src/musicdl/media/validation.py`: `normalize_language`; `tests/unit/test_media_validation.py`, `tests/unit/test_media_download.py` |
| FR-007 | A failed source is excluded from one refreshed search, never auto-selected for replacement, and health-checked once; refresh and health outcomes are returned and recorded. | `src/musicdl/media/fallback.py`: `download_with_fallback`; `tests/unit/test_media_fallback.py` |
| OPS-001 | Structured download, cleanup, refresh, and health events carry request/source/stage/status fields and stable redacted error codes; raw exception text and secrets are excluded. | `src/musicdl/media/models.py`: `DownloadEvent`; `src/musicdl/media/download.py`, `src/musicdl/media/fallback.py`; `tests/unit/test_media_download.py`, `tests/unit/test_media_fallback.py` |

## Changed files

Accepted Phase 4 implementation and test commits changed:

- `src/musicdl/media/__init__.py`
- `src/musicdl/media/models.py`
- `src/musicdl/media/validation.py`
- `src/musicdl/media/download.py`
- `src/musicdl/media/fallback.py`
- `tests/unit/test_media_validation.py`
- `tests/unit/test_media_download.py`
- `tests/unit/test_media_fallback.py`

The accepted ID3 validation fix and Windows extended-length path containment fix are included in the files listed above.

## Verification

Command run from the phase worktree:

```powershell
& '..\\..\\.venv\\Scripts\\python.exe' -m pytest -q -W error
```

Completed result: exit status `0`; `221 passed, 1 skipped`.

The formerly intermittent Windows concurrency regression was also run in ten separate completed invocations; all ten passed.

The one skipped test is `tests/integration/test_wecom_redis.py::test_real_redis_atomic_state_contract`, skipped because `MUSICDL_TEST_REDIS_URL` is not configured. Real provider/network media transfers were not exercised; download tests use injected in-memory sources and temporary filesystems.

## Rollback

The exact overall Phase 4 implementation/history range is `98be64c^..bbfe25a`; if implementation rollback is required, revert that range in reverse order. Documentation-only commits may be reverted independently. Revert this summary commit independently if needed; rollback does not delete published media or user files.
