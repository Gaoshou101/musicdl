# Phase 4 Download and Media Safety Design

## Scope

Phase 4 implements requirements FR-005, FR-006, FR-007, and OPS-001 after a user has confirmed a search candidate. It adds a source-independent download boundary, validates media before publication, writes files into the configured media root, and returns refreshed candidates after a failed source.

This phase does not add an administration UI, a plugin process runner, automatic selection of a replacement candidate, or new search providers. The existing WeCom selection state remains the authorization boundary: no file is downloaded until the selection has been consumed successfully.

## Architecture

Add a focused `musicdl.media` module. Search sources continue to implement the existing search protocol. Download-capable integrations implement a separate injected `DownloadSource` protocol so search, media transfer, validation, and health checks do not become one coupled interface.

The adapter resolves the remote object from `Candidate.source_id` and `Candidate.item_id`; URLs and credentials never enter the candidate model or logs.

The module exposes these conceptual operations:

- `download_candidate`: stream, validate, name, and atomically publish one confirmed candidate.
- `download_with_fallback`: orchestrate one download attempt and the required failure transition.
- `DownloadResult`: return the final relative path, SHA-256, byte size, media type, extension, and normalized language.
- `DownloadEvent`: a structured, redacted status record emitted through an injected recorder.

Dependencies such as the source adapter, candidate refresh function, and event recorder are injected. Tests therefore exercise real filesystem behavior without network services.

## Download Source Contract

`DownloadSource` identifies a source by its registry ID and provides:

- an asynchronous byte stream for a `Candidate`;
- trusted response metadata when available: declared size, media type, and extension;
- one asynchronous health check used only by fallback orchestration.

The download engine does not retry internally. A failed transfer yields one fallback transition, preventing duplicate downloads or repeated health probes.

## Successful Data Flow

1. Receive a confirmed `Candidate`, selected language, configured media root, adapter, and request ID.
2. Normalize language to exactly one of `华语`, `欧美`, `日韩`, or `未知`; absent or unrecognized values become `未知`.
3. Create a uniquely named temporary file inside the media root so publication can use an atomic same-filesystem rename.
4. Stream bytes while calculating SHA-256 and enforcing a hard limit of 500 MiB. Empty streams and streams exceeding the limit fail.
5. Cross-check the actual byte count against a declared size when supplied. A mismatch is treated as a partial or malformed download.
6. Detect the media type from the file signature and validate it against the declared media type and extension.
7. Sanitize the title and artist, construct the target path, and prove the resolved path remains below the media root.
8. Flush and synchronize the temporary file, then atomically rename it to a non-existing target.
9. Emit a structured success event and return `DownloadResult`.

No partially validated file is ever visible at the final path.

## Media Validation

The initial allowlist is:

| Extension | Accepted media type | Required signature check |
| --- | --- | --- |
| `.mp3` | `audio/mpeg` | ID3 header or a valid MPEG audio frame sync |
| `.flac` | `audio/flac` | `fLaC` |
| `.m4a` | `audio/mp4`, `audio/x-m4a` | ISO-BMFF `ftyp` box with an audio-compatible brand |
| `.ogg` | `audio/ogg`, `application/ogg` | `OggS` |

The detected type, declared media type, declared extension, and candidate format must agree when supplied. Unknown extensions, signature mismatches, MIME mismatches, empty files, oversized files, and declared-size mismatches fail closed.

The 500 MiB limit is a function parameter with that fixed default, allowing tests and later configuration without expanding Phase 4 configuration scope.

## Paths and Naming

Final files use this hierarchy:

`<media-root>/<language>/<artist>/<title> - <artist>.<ext>`

Path components remove control characters, replace Windows and POSIX separator/illegal characters, trim trailing dots and spaces, and reject traversal tokens. Empty results use a stable `未知` placeholder. Windows reserved device names are prefixed safely. The engine resolves and verifies the complete destination is a descendant of the configured media root before creating directories or publishing files.

Existing files are never overwritten. Collisions are resolved deterministically by probing `<title> - <artist> (2).<ext>`, then `(3)`, and so on. Publication uses exclusive reservation or an equivalent race-safe operation so concurrent writers cannot select the same target silently.

## Failure and Fallback Flow

Any transfer, size, media, path, filesystem, cancellation, or validation failure performs these actions:

1. Remove the temporary file in a `finally`-protected cleanup path.
2. Mark the selected source as failed for this fallback result.
3. Refresh candidates while explicitly excluding the failed source.
4. Invoke the failed source's health check exactly once.
5. Emit structured download-failure, refresh, and health status events.
6. Return the refreshed candidate result to the caller for a new user choice.

Fallback never downloads another candidate automatically. Refresh failure and health-check failure are represented in the returned status and events without replacing the original download failure or leaking raw response bodies.

## Observability and Secret Handling

Structured events contain only allowlisted fields: request ID, candidate ID, source ID and version, stage, status, normalized error code, byte size, SHA-256 after successful validation, relative final path, and health status.

Events never include download bytes, exception representations, complete upstream responses, credentials, API keys, sessions, authorization headers, or full URLs/query parameters. Adapter-specific exceptions are converted to stable error codes at the media boundary.

## Testing

Tests use temporary media roots and in-memory async adapters. They cover:

- each allowed language directory and normalization to `未知`;
- successful MP3, FLAC, M4A, and OGG validation and publication;
- exact final naming and deterministic collision suffixes;
- separators, traversal attempts, control characters, empty names, and Windows reserved names;
- target containment under the configured media root;
- extension spoofing, MIME mismatch, signature mismatch, unknown formats, and empty streams;
- the 500 MiB boundary with a smaller injected test limit;
- declared-size mismatch and interrupted streams;
- absence of temporary or final files after failure;
- exclusion of the failed source, refreshed candidates, and exactly one health call;
- refresh and health-check failures with redacted structured events;
- preservation of existing source-search and WeCom behavior through the full test suite.

The required verification command is:

```powershell
python -m pytest -q -W error
```

Completion requires exit code 0 with no warnings.

## Rollback

The implementation is additive and can be rolled back by reverting the Phase 4 commits. It does not migrate stored data or change existing candidate and selection formats. Temporary files use a Phase 4-specific naming convention and are removed on every handled failure; no rollback operation deletes previously published user media.
