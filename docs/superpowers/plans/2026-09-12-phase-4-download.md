# Phase 4 Download and Media Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Download a user-confirmed candidate into a validated, deterministic media path and return safe refreshed candidates when the selected source fails.

**Architecture:** Add a focused `musicdl.media` package with separate models/protocols, validation and path helpers, atomic download publication, and fallback orchestration. Download sources, candidate refresh, and event recording are injected so the behavior is deterministic and network-free in tests.

**Tech Stack:** Python 3.12, standard-library asyncio/pathlib/hashlib/tempfile/os, dataclasses and protocols, pytest 9.1.

**Spec:** `docs/superpowers/specs/2026-09-12-phase-4-download-design.md`

## Global Constraints

- Supported extensions are exactly `.mp3`, `.flac`, `.m4a`, and `.ogg` with the MIME/signature rules in the spec.
- The default hard maximum is exactly 500 MiB (`500 * 1024 * 1024` bytes).
- Language is exactly one of `华语`, `欧美`, `日韩`, or `未知`; every other input normalizes to `未知`.
- Final paths are `<media-root>/<language>/<artist>/<title> - <artist>.<ext>` and must remain below `media_root`.
- Existing files are never overwritten; collisions use ` (2)`, ` (3)`, and subsequent deterministic suffixes.
- Fallback excludes the failed source, refreshes candidates, checks that source exactly once, and never chooses another candidate automatically.
- Structured events must not contain response bodies, URLs, credentials, API keys, sessions, authorization headers, or raw exception text.
- Do not add production dependencies or modify `.planning/ROADMAP.md` or `.planning/phases/02-source-search/SUMMARY.md`.

## File Structure

- Create `src/musicdl/media/__init__.py`: stable public exports only.
- Create `src/musicdl/media/models.py`: immutable transfer metadata, result, event, fallback result, protocols, and stable exceptions.
- Create `src/musicdl/media/validation.py`: language and filename normalization, containment, extension/MIME/signature validation.
- Create `src/musicdl/media/download.py`: bounded streaming, hashing, temporary-file cleanup, collision-safe atomic publication.
- Create `src/musicdl/media/fallback.py`: single-attempt orchestration, failed-source exclusion, refresh, one health check, and redacted events.
- Create `tests/unit/test_media_validation.py`: pure validation and path tests.
- Create `tests/unit/test_media_download.py`: real temporary-filesystem download tests.
- Create `tests/unit/test_media_fallback.py`: injected-adapter fallback state and event tests.

---

### Task 1: Media Contracts, Path Safety, and Signature Validation

**Files:**
- Create: `src/musicdl/media/__init__.py`
- Create: `src/musicdl/media/models.py`
- Create: `src/musicdl/media/validation.py`
- Test: `tests/unit/test_media_validation.py`

**Interfaces:**
- Consumes: `musicdl.sources.models.Candidate`.
- Produces: `MAX_MEDIA_BYTES`, `Language`, `DownloadMetadata`, `DownloadSource`, `DownloadResult`, `DownloadEvent`, `FallbackResult`, `MediaError`, `normalize_language`, `sanitize_component`, `validated_destination`, and `validate_media`.

- [ ] **Step 1: Write failing contract and path tests**

Create tests that import the public package and assert:

```python
assert normalize_language("华语") == "华语"
assert normalize_language("other") == "未知"
assert sanitize_component(" ../AUX/song\x00 ") == "_AUX_song"
path = validated_destination(tmp_path, "华语", "A/B", "../Song", ".mp3")
assert path.relative_to(tmp_path).parts == ("华语", "A_B", "Song - A_B.mp3")
```

Also assert `DownloadMetadata` rejects no values by construction but `validate_media` rejects an unknown extension, an empty header, and a target escape attempt with stable `MediaError.code` values.

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest tests/unit/test_media_validation.py -q -W error`

Expected: collection fails with `ModuleNotFoundError: No module named 'musicdl.media'`.

- [ ] **Step 3: Implement immutable contracts and path helpers**

Implement these exact shapes in `models.py`:

```python
MAX_MEDIA_BYTES = 500 * 1024 * 1024
Language = Literal["华语", "欧美", "日韩", "未知"]

@dataclass(frozen=True)
class DownloadMetadata:
    chunks: AsyncIterable[bytes]
    extension: str | None = None
    media_type: str | None = None
    declared_size: int | None = None

class DownloadSource(Protocol):
    async def download(self, candidate: Candidate) -> DownloadMetadata: ...
    async def health(self) -> bool: ...

@dataclass(frozen=True)
class DownloadResult:
    relative_path: Path
    sha256: str
    size_bytes: int
    media_type: str
    extension: str
    language: Language

@dataclass(frozen=True)
class DownloadEvent:
    request_id: str
    candidate_id: str
    source_id: str
    source_version: str
    stage: str
    status: str
    error_code: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    relative_path: str | None = None
    healthy: bool | None = None

class MediaError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
```

`FallbackResult` contains `download: DownloadResult | None`, `refreshed: SearchResult | None`, `failed_source_id: str | None`, `download_error: str | None`, `refresh_error: str | None`, and `healthy: bool | None`.

Implement path helpers with a fixed illegal-character regex, Windows reserved-name protection, normalized language, lowercase dotted extensions, `Path.resolve(strict=False)` containment, and no directory creation.

- [ ] **Step 4: Add RED signature tests, then implement exact allowlist**

Parametrize valid headers for ID3 MP3, MPEG-frame MP3, FLAC, M4A `ftypM4A `, and OGG. Assert declared extension, candidate format, and declared MIME mismatches raise `MediaError` with `unsupported_extension`, `extension_mismatch`, `mime_mismatch`, or `signature_mismatch`.

Run the focused parametrized tests before implementation and observe failure, then implement `validate_media(header, metadata, candidate_format) -> tuple[str, str]` using the spec table.

- [ ] **Step 5: Run Task 1 tests and commit**

Run: `python -m pytest tests/unit/test_media_validation.py -q -W error`

Expected: all tests pass, exit code 0.

Commit only Task 1 files with message: `feat: add safe media validation contracts`.

### Task 2: Bounded Atomic Download Publication

**Files:**
- Modify: `src/musicdl/media/__init__.py`
- Modify: `src/musicdl/media/models.py`
- Modify: `src/musicdl/media/validation.py`
- Create: `src/musicdl/media/download.py`
- Test: `tests/unit/test_media_download.py`

**Interfaces:**
- Consumes: Task 1 contracts and helpers.
- Produces: `async download_candidate(candidate: Candidate, source: DownloadSource, media_root: str | Path, *, request_id: str, language: str | None = None, max_bytes: int = MAX_MEDIA_BYTES, record: Callable[[DownloadEvent], None] | None = None) -> DownloadResult`.

- [ ] **Step 1: Write and run failing successful-download tests**

Use a real `tmp_path` and an adapter returning an async chunk generator. Assert a valid MP3 publishes exactly `华语/Artist/Song - Artist.mp3`, content and SHA-256 match, result path is relative, success event contains only typed fields, and no Phase 4 temp files remain.

Run: `python -m pytest tests/unit/test_media_download.py::test_download_publishes_valid_media_atomically -q -W error`

Expected: FAIL because `download_candidate` is missing.

- [ ] **Step 2: Implement the minimal successful pipeline**

Validate `max_bytes` as a positive non-bool integer. Create the media root, create a unique `.musicdl-*.part` file directly below it, stream only non-empty `bytes` chunks, hash and count, retain enough header bytes for signature detection, flush and `os.fsync`, validate size and media, compute the safe destination, create only its language/artist parents, and publish with a race-safe non-overwriting operation. Return `DownloadResult` and record one `download/success` event.

- [ ] **Step 3: Write RED failure and cleanup tests**

Add separate tests for empty stream, non-byte chunk, declared-size mismatch, injected max-byte overflow, extension/MIME/signature mismatch, and a generator that raises `RuntimeError("token=secret")`. Each test asserts stable error code, no final media, no `.musicdl-*.part`, and no raw exception text in recorded events.

Run the new tests and confirm each fails for missing behavior, not test setup.

- [ ] **Step 4: Implement fail-closed cleanup**

Convert adapter acquisition/stream exceptions to `MediaError("download_failed")`, invalid chunks to `invalid_chunk`, zero bytes to `empty_download`, oversize to `file_too_large`, and size mismatch to `size_mismatch`. Emit one redacted `download/failed` event and unlink the temporary file in `finally`, including cancellation-safe cleanup while re-raising `CancelledError`.

- [ ] **Step 5: Write RED collision and format-matrix tests, then implement**

Parametrize MP3/FLAC/M4A/OGG success. Pre-create the canonical file and assert subsequent downloads publish ` (2)` then ` (3)` without changing prior content. Implement deterministic collision probing with exclusive target reservation or hard-link/rename semantics that cannot overwrite an existing target; clean any reservation on failure.

- [ ] **Step 6: Run Task 1 and Task 2 tests and commit**

Run: `python -m pytest tests/unit/test_media_validation.py tests/unit/test_media_download.py -q -W error`

Expected: all tests pass, exit code 0, no warnings.

Commit Task 2 files with message: `feat: publish validated downloads atomically`.

### Task 3: Failed-Source Fallback and Redacted Status

**Files:**
- Modify: `src/musicdl/media/__init__.py`
- Create: `src/musicdl/media/fallback.py`
- Test: `tests/unit/test_media_fallback.py`

**Interfaces:**
- Consumes: `download_candidate`, `Candidate`, `SearchResult`, `DownloadSource`, `DownloadEvent`, and `FallbackResult`.
- Produces: `async download_with_fallback(candidate: Candidate, sources: Mapping[str, DownloadSource], media_root: str | Path, *, request_id: str, query: str, refresh: Callable[[str, frozenset[str]], Awaitable[SearchResult]], language: str | None = None, max_bytes: int = MAX_MEDIA_BYTES, record: Callable[[DownloadEvent], None] | None = None) -> FallbackResult`.

- [ ] **Step 1: Write and run the RED success passthrough test**

Assert a valid selected source returns `FallbackResult.download`, does not call `refresh` or `health`, and records the download success event.

Run: `python -m pytest tests/unit/test_media_fallback.py::test_success_does_not_enter_fallback -q -W error`

Expected: FAIL because `download_with_fallback` is missing.

- [ ] **Step 2: Implement the success path**

Resolve the adapter by exact `candidate.source_id`; a missing adapter becomes stable `source_unavailable`. Delegate one attempt to `download_candidate` and return immediately on success.

- [ ] **Step 3: Write RED fallback state-transition tests**

Make download fail and assert `refresh(query, frozenset({candidate.source_id}))` is called once, returned candidates contain no failed source, `health()` is called exactly once, no replacement download occurs, and result fields preserve the original stable error. Assert events appear in order: download failed, refresh success/failed, health success/failed.

- [ ] **Step 4: Implement one-shot fallback**

Catch only stable media/source failures, call refresh once with the exclusion set, reject a refresh result that still contains the failed source using `refresh_included_failed_source`, then call health once regardless of refresh outcome. Convert refresh exceptions to `refresh_failed` and health exceptions to `health_failed` without embedding exception text. Return all observable statuses in `FallbackResult`.

- [ ] **Step 5: Add and satisfy redaction/error tests**

Use exceptions containing fake URL query credentials, session names, and API keys. Assert neither `str(FallbackResult)` nor any event dictionary contains those values. Assert missing source also follows refresh and records health as unavailable without attempting a method call.

- [ ] **Step 6: Run focused and full verification**

Run: `python -m pytest tests/unit/test_media_fallback.py -q -W error`

Expected: all fallback tests pass.

Run: `python -m pytest -q -W error`

Expected: the complete suite passes with exit code 0 and no warnings.

- [ ] **Step 7: Review scope and commit**

Run `git diff --check`, review every Phase 4 source/test diff, and confirm `.planning/ROADMAP.md` and `.planning/phases/02-source-search/SUMMARY.md` are absent from the staged set.

Commit Task 3 files with message: `feat: add safe download fallback orchestration`.

### Task 4: Final Acceptance and Documentation Accuracy

**Files:**
- Modify only if implementation facts require it: `README.md`
- Modify only if acceptance evidence is recorded: `.planning/phases/04-download-media-safety/SUMMARY.md`

**Interfaces:**
- Consumes: all completed Phase 4 behavior and full-suite evidence.
- Produces: accurate operator-facing feature description and traceable acceptance evidence; no new runtime behavior.

- [ ] **Step 1: Check documentation claims against implementation**

Verify README's “Current implementation” statement no longer says download/media validation remains a later phase. If it does, replace only that sentence with an accurate statement that Phase 4 provides the injected download/media-safety engine while concrete deployment adapters remain integration work.

- [ ] **Step 2: Re-run final verification after documentation changes**

Run: `python -m pytest -q -W error`

Expected: complete suite passes, exit code 0, no warnings.

- [ ] **Step 3: Record evidence and commit**

Create the Phase 4 summary only after successful verification. Record requirements satisfied, exact changed files, the complete test command and result, limitations, rollback commit range, and explicitly note that real provider/network transfer was not exercised.

Stage only README and the Phase 4 summary, then commit with message: `docs: record phase 4 verification`.

- [ ] **Step 4: Sol acceptance and synchronization**

Sol reviews the real commit range, changed-file scope, diff, and fresh full-suite output. Only after acceptance, push `codex/phase-4-download` to `origin` without force.
