# Plugin Download Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver plugin search, one-time selection, strict media resolution, main-owned secure streaming, Phase 4 validation/archive, and WeCom notification as one testable vertical slice with generation-safe, durable failure recovery.

**Architecture:** Keep `PluginClient.invoke()` as the bounded subprocess-runner JSON boundary and add typed `resolve()`/`health()` helpers on top. `PluginSource` resolves metadata only; a new `musicdl.media.transport.SecureMediaTransport` fetches bytes in the main process and returns the existing `musicdl.media.models.DownloadMetadata` stream to Phase 4. `RedisStateStore` atomically stores the canonical full selection snapshot and owns a durable, fenced job-effect state machine; `JobWorker` advances that machine, reserves deterministic artifact paths, and replays completed artifacts without repeating external side effects.

**Tech Stack:** Python 3.12, Pydantic 2, httpx for the runner control client, synchronous `socket`/`ssl`/`http.client` primitives inside an async transport boundary, Redis Streams/Lua through the existing `redis` client, FastAPI runtime assembly, and pytest 9.

**Spec:** `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md`, `docs/superpowers/specs/2026-09-12-phase-4-download-design.md`, and `docs/superpowers/specs/2026-09-12-phase-6-plugin-runtime-design.md`.

## Global Constraints

- Preserve the `musicdl.plugin/v1` envelope and existing `Operation` values; `resolve` and `health` are typed operations, while `download` remains compatibility-only for this slice.
- Keep plugin request/result JSON at 64 KiB (`MAX_PAYLOAD_BYTES`), source/action invocation bodies at their existing limits (128 KiB source, 1 MiB action body, 6 MiB invocation), and media streaming at `MAX_MEDIA_BYTES = 500 * 1024 * 1024`.
- `ResolvedMedia` has exactly `candidate_id`, `url`, `extension`, `media_type`, and optional `declared_size`; it is frozen and rejects unknown fields, credentials, fragments, explicit ports, non-HTTPS URLs, controls, and mismatched extension/MIME.
- Streaming is main-owned. Plugin stdin/stdout carries descriptors and JSON metadata only; media bytes and base64 never cross the runner boundary.
- DNS-pinned HTTPS accepts only an exact IDNA-normalized manifest host, rejects every non-global A/AAAA answer, connects to a numeric IP with approved hostname SNI/`Host`, disables proxies/redirects/ambient headers, and closes every response path.
- A resolved descriptor is usable only when `resolved.candidate_id == Candidate.item_id`; a job stores the immutable selected candidate or equivalent generation before download work.
- A stable Redis-stream `job_id` owns a durable fenced effect state machine, not a `SET NX` marker. A live lease is observed as in-progress and is never retried by a second worker; a lease-expired worker may resume only from the last durable stage with a higher fence, stale fences cannot commit, and an external call with unknown outcome becomes a terminal uncertain stage rather than being replayed. Failure does one refresh excluding `frozenset({failed_source_id})`, one health call, one persisted rebind, and no automatic replacement download.
- Selection tokens atomically contain canonical full `Candidate` snapshots plus `corp_id`, `from_user`, `request_id`, query, content-hash version, and integer selection generation; user/request indexes point at that token and never become the source of a mutable candidate lookup.
- Artifact reservation is deterministic and durable. The owner/fence CAS artifact states are `prepared -> external_started -> stream_complete -> publishing -> physically_published -> published`; `reserved` is not a state. Before any source call, `prepare_artifact()` acquires a per-base fenced allocation lock, reconciles a durable sidecar slot ledger with filesystem occupancy (including preexisting files), assigns slot 0 then slots 2, 3, …, and persists the chosen slot plus exact POSIX-relative temporary/target paths in the `ArtifactRecord`. The chosen path is immutable on redelivery: recovery never probes a new suffix, scans sibling targets, or infers ownership. No final path is visible until EOF, fsync, SHA-256, byte count, declared size, signature, and MIME/extension checks are durably checkpointed. A redelivery verifies/replays only the persisted target path or validates the reservation-owned temporary file; it never blindly downloads the same artifact again. An incomplete/ambiguous reservation is terminal rather than an unsafe retry.

  **Artifact takeover/recovery matrix (owner/fence CAS):** A live lease returns `owner_conflict`; only an expired lease may be taken over with a strictly higher fence. An expired `prepared` record may resume only when the `download` effect has not reached `external_started` and both its reserved temp and target are absent. A `prepared` record with an expired `external_started` effect, or with either file already present but no durable stream checkpoint, becomes `uncertain` and is never replayed. A `stream_complete` record may be taken over with a higher fence only after exact checkpointed-temp verification, then transitions through `publishing` and is published to its persisted target without a source call. A `publishing` or `physically_published` record is taken over with a higher fence by verifying only the exact persisted target, then CAS-completing publication; a `published` record is likewise verified and replayed without a source call. Every stale owner/fence is blocked from mutating, deleting, or publishing through artifact/ledger CAS.
- WeCom notification effects are separate fenced stages named `success_notice`, `selection_prompt`, and `terminal_failure_notice`. A known acknowledgement completes the matching effect; an unknown send outcome records an effect-specific uncertain terminal (`success_notice_uncertain`, `prompt_uncertain`, or `terminal_failure_notice_uncertain`) and is never auto-resubmitted. The job is acknowledged as terminal and the user must issue a new search (or an operator must reconcile state) before another prompt is sent.
- The complete `JobWorker.handle_job()` deadline includes all Redis state/lease overhead, media/artifact work, and WeCom delivery; each `send_text` call is capped at the existing 10-second WeCom client timeout and at the remaining handler deadline. Effect leases are derived from the remaining handler deadline plus a bounded Redis-overhead allowance, renewed and owner/fence-verified immediately before every external call, and never extend the handler deadline.
- Time units are explicit: `search_timeout`, `resolve_stream_timeout`, `health_timeout`, `job_timeout`, `budget_slack_seconds`, `retry_window_seconds`, and `job_ttl` are seconds; `pending_idle_ms` is milliseconds. Enforce `resolve_stream_timeout + search_timeout + health_timeout < job_timeout` with at least `budget_slack_seconds` of material slack, `pending_idle_ms > 1000 * (max(search_timeout, job_timeout) + redis_overhead_seconds)`, `retry_window_seconds > max_attempts * ceil(pending_idle_ms / 1000)`, and `job_ttl > retry_window_seconds + ceil(job_timeout)`.
- Use existing packages and repository `.venv`; no production dependency, provider, Telegram wiring, AI language classifier, admin UI, CI/deployment, Docker/Compose, or real-network acceptance is added.

---

## File map and execution topology

The existing modules are deliberately retained. New code is limited to the explicitly marked `Create` paths below; every existing path is marked `Modify`. Three direct `gpt-5.6-luna` workers (reasoning `max`) may execute useful independent tasks per wave and may not delegate. Shared-file edits are serialized, Sol reviews the settled real diff independently after each wave, and final verification waits for all edits to settle.

- Wave 1: Task 1 (contract), Task 2 (transport/lifecycle), and Task 3 (typed client). Task 2 imports the Task 1 model only after the contract edit is settled.
- Wave 2: Task 4 (source adapter), Task 5 (state/worker/fallback), and Task 6 (config/runtime). Serialize constructor/signature edits in `src/musicdl/worker/workers.py` and `src/musicdl/app.py`.
- Wave 3: Task 7 (deterministic integration fixture) and Task 8 (docs/acceptance), with the third Luna doing read-only cross-task security/type review. Run the integration command only after Waves 1–2 are complete.
- Each worker reports changed paths, exact interfaces, completed RED/GREEN command and exit status, risks, and a scoped commit. A worker stops on ambiguity or out-of-scope migration. No worker changes this plan file.

## Task 1: Add the strict resolved-media contract

**Files:**

- Modify: `src/musicdl/contracts/plugin.py` — add the descriptor model and extension/MIME constants beside the existing `PluginRequest`/`PluginResponse` models.
- Modify: `src/musicdl/contracts/__init__.py` — export `ResolvedMedia` and its public constants.
- Modify (tests): `tests/unit/test_plugin_contract.py` — add descriptor schema and rejection cases to the existing contract tests.

**Interfaces:**

- Consumes: existing `Operation`, `Candidate.item_id`, `MAX_PAYLOAD_BYTES`, and the accepted media signatures in `src/musicdl/media/validation.py`.
- Produces: `ResolvedMedia` for Task 3 and Task 4; `RESOLVED_MEDIA_MAX_BYTES` equal to `MAX_MEDIA_BYTES` without importing a media implementation into the plugin protocol.

```python
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

ResolvedExtension = Literal["mp3", "flac", "m4a", "ogg"]
RESOLVED_MEDIA_MAX_BYTES = 500 * 1024 * 1024
RESOLVED_MEDIA_TYPES = {
    "mp3": frozenset({"audio/mpeg"}),
    "flac": frozenset({"audio/flac"}),
    "m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    "ogg": frozenset({"audio/ogg", "application/ogg"}),
}

class ResolvedMedia(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_id: StrictStr = Field(min_length=1, max_length=256)
    url: StrictStr = Field(min_length=1, max_length=4096)
    extension: ResolvedExtension
    media_type: StrictStr
    declared_size: StrictInt | None = Field(default=None, ge=0, le=RESOLVED_MEDIA_MAX_BYTES)

    @field_validator("url", mode="before")
    @classmethod
    def url_is_https_without_unsafe_parts(cls, value: str) -> str:
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
            raise ValueError("URL contains controls or whitespace")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("malformed HTTPS URL") from exc
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.fragment or port is not None):
            raise ValueError("URL must be implicit-port HTTPS without credentials or fragment")
        return value

    @model_validator(mode="after")
    def media_type_matches_extension(self):
        if self.media_type.casefold() not in RESOLVED_MEDIA_TYPES[self.extension]:
            raise ValueError("media_type does not match extension")
        return self
```

- [ ] **Step 1: Write the failing tests.** Extend `test_plugin_contract.py` with valid descriptors for all four formats and parametrized rejection of unknown fields (`headers`, `data`, `bytes`), HTTP/non-HTTPS schemes, userinfo, fragments, explicit ports (including `:443`), controls/whitespace, bad MIME/extension pairs, negative/over-limit sizes, and frozen mutation.
- [ ] **Step 2: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_plugin_contract.py -q
  ```

  Expected: FAIL because `ResolvedMedia` is not defined or accepts one of the forbidden values.
- [ ] **Step 3: Implement the smallest model.** Add the exact fields and validators above, use `StrictInt` so booleans/floats/strings cannot become `declared_size`, lower-case only for MIME comparison, preserve the raw URL string, and keep `model_dump_json()` limited to those five fields.
- [ ] **Step 4: Run GREEN.** Rerun the same command; expected `N passed` and exit status 0. Also run:

  ```powershell
  .venv\Scripts\python.exe -c "from musicdl.contracts.plugin import ResolvedMedia; print(ResolvedMedia.model_config['extra'], ResolvedMedia.model_config['frozen'])"
  ```

  Expected output: `forbid True` (or equivalent Pydantic representation).
- [ ] **Step 5: Commit.**

  ```powershell
  git add src/musicdl/contracts/plugin.py src/musicdl/contracts/__init__.py tests/unit/test_plugin_contract.py
  git commit -m "feat: add strict resolved media contract"
  ```

Rollback only this scoped commit if an import conflict is found; never relax descriptor validation to accommodate later code.

## Task 2: Add main-owned secure streaming and close-safe metadata

**Files:**

- Create: `src/musicdl/media/transport.py` — own DNS-pinned raw HTTPS streaming and return the existing `DownloadMetadata` shape.
- Modify: `src/musicdl/media/models.py` — add a task/lock-backed close hook to the existing `DownloadMetadata(chunks, extension, media_type, declared_size)` dataclass and define the `ArtifactRecord`/`ArtifactStore` protocol; do not replace metadata with a status-only object.
- Modify: `src/musicdl/media/download.py` — close metadata on every path and accept deterministic artifact reservations for publication/replay.
- Modify: `src/musicdl/plugins/broker.py` — extract shared URL/IDNA/global-address policy helpers used by both action fetching and media streaming; keep media outside `HttpsActionBroker.fetch()`.
- Modify (tests): `tests/unit/test_media_download.py` — add close lifecycle cases to the existing download tests.
- Create (tests): `tests/unit/test_media_transport.py` — fake DNS/socket/TLS/HTTP security tests.

**Interfaces:**

- Consumes: `ResolvedMedia`, `PluginManifest.allowed_hosts`, `MAX_MEDIA_BYTES`, and existing `DownloadMetadata` fields.
- Produces:

  ```python
  import asyncio
  import http.client
  import socket
  import ssl
  import time
  from collections.abc import Awaitable, Callable, AsyncIterable, Iterable
  from dataclasses import dataclass, field
  from pathlib import Path
  from typing import Literal, Protocol

  MAX_RESPONSE_HEADER_COUNT = 64
  MAX_RESPONSE_HEADER_FIELD_BYTES = 8 * 1024
  MAX_RESPONSE_HEADERS_BYTES = 64 * 1024

  @dataclass
  class _CloseOnce:
      closer: Callable[[], Awaitable[None]]
      _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
      _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

      async def close(self) -> None:
          async with self._lock:
              if self._task is None:
                  self._task = asyncio.create_task(self.closer())
              task = self._task
          try:
              await asyncio.shield(task)
          except asyncio.CancelledError as cancellation:
              # Drain the same shielded task. A close failure (or a second
              # cancellation) cannot replace the caller's cancellation.
              while not task.done():
                  try:
                      await asyncio.shield(task)
                  except asyncio.CancelledError:
                      continue
                  except BaseException:
                      break
              if task.done():
                  try:
                      task.result()
                  except BaseException:
                      pass
              raise cancellation

  @dataclass(frozen=True)
  class DownloadMetadata:
      chunks: AsyncIterable[bytes]
      extension: str | None = None
      media_type: str | None = None
      declared_size: int | None = None
      _close_once: _CloseOnce | None = field(default=None, repr=False, compare=False)

      async def aclose(self) -> None:
          if self._close_once is not None:
              await self._close_once.close()

      async def close(self) -> None:
          await self.aclose()

  @dataclass(frozen=True)
  class ArtifactRecord:
      job_id: str
      candidate_id: str
      temporary_relative_path: str  # POSIX relative path only
      target_relative_path: str  # POSIX relative path only
      allocation_slot: int
      extension: str
      media_type: str
      declared_size: int | None = None
      size_bytes: int | None = None
      sha256: str | None = None
      owner: str | None = None
      fence: int = 0
      lease_until_ms: int | None = None
      state: Literal["prepared", "external_started", "stream_complete", "publishing", "physically_published", "published", "uncertain"] = "prepared"

  ArtifactTakeoverAction = Literal[
      "resume", "uncertain", "verify_publish", "verify_complete", "replay", "owner_conflict",
  ]

  class ArtifactStore(Protocol):
      async def prepare_artifact(
          self,
          job_id: str,
          candidate: Candidate,
          *,
          media_root: str | Path,
          base_relative_path: str,
          extension: str,
          media_type: str,
          declared_size: int | None,
          owner: str,
          fence: int,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def record_stream_complete(
          self,
          job_id: str,
          *,
          owner: str,
          fence: int,
          size_bytes: int,
          sha256: str,
          extension: str,
          media_type: str,
          declared_size: int | None,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def mark_artifact_published(
          self,
          job_id: str,
          *,
          owner: str,
          fence: int,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def takeover_artifact(
          self,
          job_id: str,
          *,
          new_owner: str,
          new_fence: int,
          now_ms: int,
          lease_ms: int,
      ) -> ArtifactTakeoverAction: ...
      async def claim_artifact_publish(
          self,
          job_id: str,
          *,
          owner: str,
          fence: int,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def get_artifact(self, job_id: str) -> ArtifactRecord | None: ...

  class SecureMediaTransport:
      def __init__(
          self,
          *,
          resolver: Callable[[str, int], Iterable[tuple]] | None = None,
          connector: Callable[[tuple, float], socket.socket] | None = None,
          tls_wrap: Callable[[socket.socket, str], socket.socket] | None = None,
          response_factory: Callable[[socket.socket], http.client.HTTPResponse] | None = None,
          clock: Callable[[], float] = time.monotonic,
          default_timeout_ms: int = 30_000,
          max_bytes: int = 500 * 1024 * 1024,
          chunk_size: int = 64 * 1024,
      ): ...
      async def open(
          self,
          media: ResolvedMedia,
          *,
          allowed_hosts: Iterable[str],
          timeout_ms: int | None = None,
      ) -> DownloadMetadata: ...
      async def aclose(self) -> None: ...

  async def download_candidate(
      candidate: Candidate,
      source: DownloadSource,
      media_root: str | Path,
      *,
      request_id: str,
      reservation: ArtifactRecord | None = None,
      artifact_store: ArtifactStore | None = None,
      owner: str | None = None,
      fence: int | None = None,
      language: str | None = None,
      max_bytes: int = MAX_MEDIA_BYTES,
      record: Callable[[DownloadEvent], None] | None = None,
  ) -> DownloadResult: ...
  ```

  `DownloadMetadata` remains the object consumed by `download_candidate`; its async `chunks`, extension, MIME, and declared size are preserved. The private response handle behind `aclose()` is closed at most once by the task/lock-backed `_CloseOnce`. `ArtifactRecord` stores only the allocation slot, validated POSIX-relative temporary/target paths, candidate identity, extension/MIME, declared and observed size, SHA-256, owner, fence, lease deadline, and the `prepared`/`external_started`/`stream_complete`/`publishing`/`physically_published`/`published` state. `ArtifactStore` is the durable owner/fence CAS boundary for those states and for the per-base allocation lock/sidecar reconciliation. The allocation ledger is per base (slot -> job and job -> slot), is reconciled against exact filesystem occupancy before allocation, and is persisted together with the artifact record. `takeover_artifact(new_owner, new_fence, now_ms, lease_ms)` returns the exact recovery action from the matrix; `claim_artifact_publish()` is the owner/fence CAS from `stream_complete` to `publishing`. A new fence must be strictly greater, a live lease returns `owner_conflict`, and stale owners cannot mutate, delete, or publish anything. Constructor seams are mandatory for tests: resolver, connector, TLS wrapper, response factory, monotonic clock, timeout, byte limit, and chunk size are injected or bounded by the defaults above.

- [ ] **Step 1: Write failing transport and lifecycle tests.** In `test_media_transport.py`, inject every constructor seam and fake resolver answers with global and private A/AAAA records; assert any non-global answer rejects the request, exact IDNA host matching, numeric-IP connect, hostname SNI/`Host`, and an exact request of `GET path[?query] HTTP/1.1` with only `Host`, `Accept`, `Accept-Encoding: identity`, and `Connection: close`, an empty body, no proxy/redirect/ambient headers, no more than 64 response headers, no header field over 8 KiB, no aggregate headers over 64 KiB, bounded body, and closure. In `test_media_download.py`, use a metadata close counter for success, stream error, validation error, cancellation, and two concurrent `aclose()` calls; the counter must be one.
- [ ] **Step 2: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_media_transport.py tests/unit/test_media_download.py -q
  ```

  Expected: FAIL because the transport and `DownloadMetadata.aclose()` lifecycle do not yet exist.
- [ ] **Step 3: Implement policy and transport.** Keep ownership explicit: `ResolvedMedia`/`PluginClient.resolve()` rejects URL syntax and schema violations as `plugin_resolve_invalid`; `SecureMediaTransport` applies only defense-in-depth URL parsing (`media_url_denied`), exact normalized manifest-host matching, and DNS/address policy; `HttpsActionBroker` reuses the private helpers only for declarative action URLs. In `transport.py`, validate `default_timeout_ms`, `max_bytes <= MAX_MEDIA_BYTES`, and positive `chunk_size`; connect to each approved numeric address with the injected connector, wrap with the injected TLS function using `server_hostname=approved_host`, and send exactly the fixed headers above with no request body. Run every blocking DNS, connect, TLS, request, response-header, body-read, and close operation through `asyncio.to_thread` with the single remaining deadline. Reject redirects, non-2xx responses, duplicate singleton or unsafe selected headers, header count/field/aggregate limits, non-identity content encoding, invalid/undeclared content type, invalid/over-limit content length, and streams that exceed the limit. Expose a bounded async iterator backed by `http.client.HTTPResponse.read()` and build `DownloadMetadata` with the descriptor’s extension/MIME/size and an idempotent response closer. On `CancelledError`, mark the opening operation aborted, start one shielded close task for every handle already acquired, and ensure a handle that arrives late from a cancelled `to_thread` is closed before re-raising; a close error never replaces the primary error.
- [ ] **Step 4: Add deterministic reservation and close ownership.** Let the worker call `ArtifactStore.prepare_artifact()` with the owner/fence, sanitized base POSIX-relative path, and `media_root`; derive and revalidate every path under `media_root`, reject absolute/drive/UNC/`..` paths and symlink parents, and create the exclusive `0600`/no-follow temp path `.musicdl-staging/<sha256(job_id)[:40]>.<fence>.part`. Under a short per-base fenced allocation lock, reconcile the durable sidecar slot ledger with exact filesystem occupancy (preexisting base, `(2)`, or any other slot files count as occupied), choose slot 0 for the unsuffixed target then slots 2, 3, …, and atomically persist `job_id -> allocation_slot` plus the exact temporary/target relative paths before any source call. The Redis ledger and artifact record retain that claim across fences; release is allowed only by a safe pre-publication cleanup CAS. The returned record and path are reused on every redelivery; never probe a new suffix, scan siblings, or infer ownership after this allocation. If the record is `published`, verify only its persisted target’s SHA-256, byte size, candidate identity, extension, and MIME and replay `DownloadResult` without opening a source. If it is `stream_complete`, a higher-fence owner verifies the exact reservation-owned temp file and publishes it to the persisted target without a source call. If it is only `prepared`, `takeover_artifact()` may resume with a strictly higher fence only when the `download` effect has not entered `external_started` and both reserved temp and target are absent; otherwise an expired `external_started` effect or any unexpected file without a durable stream checkpoint becomes `uncertain` and the source is never replayed. Immediately before `source.download(candidate)`, CAS the artifact `prepared -> external_started`; after EOF enforce the hard/declared size limits, validate signature and MIME/extension, flush and `fsync`, close the file, then CAS `external_started -> stream_complete` through `record_stream_complete()` with the owner/fence and exact hash/size/MIME checkpoint. Claim publication with `claim_artifact_publish()` (`stream_complete -> publishing`) before using an exclusive same-filesystem `os.link(temp, target)` (or a platform no-replace equivalent that cannot overwrite); `fsync` the target and parent directory, then CAS `publishing -> physically_published` and `physically_published -> published` through `mark_artifact_published()` with the same owner/fence. If a crash leaves a `publishing` or `physically_published` record, a higher-fence owner verifies only the exact persisted target and CAS-completes publication; it does not scan derived paths. Never make a partial temp visible at the target or publish a temp lacking that durable EOF/fsync/hash/size/MIME checkpoint. A partial/missing temp or an exact-target mismatch without a durable checkpoint is `artifact_uncertain`, never a new suffix or redownload. Keep `metadata` in a local initialized to `None`; after `source.download(candidate)`, put the existing stream/validation/publication logic under `try`, and in `finally` create or reuse one shielded close task for `metadata.aclose()`. If cancellation arrives while closing, finish that same task, then re-raise the original `CancelledError`; if close itself fails while another exception is active, preserve the primary error and emit only the stable redacted close/download code. Preserve existing redacted `MediaError` events.
After `record_stream_complete()` returns, reopen/stat/hash the closed temp and compare the exact checkpointed size, SHA-256, detected MIME, declared MIME, extension, and candidate format before linking it to the persisted target; any mismatch becomes `artifact_uncertain` and is never published. Every artifact, allocation, cleanup, and publication mutation is owner/fence CAS; stale owners are blocked from mutating, deleting, or publishing.

- [ ] **Step 5: Run GREEN.** Rerun the command; expected `N passed`, exit status 0, and a close counter of one on every terminal path. Assert deterministic reservation replay does not invoke `source.download`, no response object or media bytes is sent to a plugin runner, cancellation cannot leave the response open, and a handle returned late by a cancelled blocking operation is closed before the task exits.
- [ ] **Step 6: Commit.**

  ```powershell
  git add src/musicdl/media/transport.py src/musicdl/media/models.py src/musicdl/media/download.py src/musicdl/plugins/broker.py tests/unit/test_media_transport.py tests/unit/test_media_download.py
  git commit -m "feat: stream resolved media through main transport"
  ```

If the available HTTP primitive cannot provide numeric-IP connect with hostname SNI, keep the same constructor seams and adapt the low-level socket client; do not fall back to ordinary URL fetching. A live stream whose owner is cancelled is closed before the cancellation escapes; there is no background retry.

## Stable error and cancellation table

All boundaries below expose only the listed code to callers/events. Raw URLs, response bodies, credentials, exception text, and runner output are never included. `download_cancelled` always re-raises `asyncio.CancelledError`, closes the stream, and skips refresh/health/rebind.

URL error ownership is deliberately split: `ResolvedMedia` and `PluginClient.resolve()` own descriptor-schema and URL-syntax validation, so controls, whitespace, credentials, fragments, explicit ports, malformed URLs, and non-HTTPS schemes become `plugin_resolve_invalid` before transport is opened. `SecureMediaTransport` receives only that validated model and owns defense-in-depth URL parsing (`media_url_denied` if an impossible invariant is presented), exact manifest-host matching (`media_host_denied`), DNS lookup (`media_dns_failed`), and address policy (`media_address_denied`). `HttpsActionBroker` uses the same private policy helpers for action URLs but does not classify plugin media resolution errors.

| Boundary/condition | Stable code | Event/result behavior |
| --- | --- | --- |
| Plugin resolve returned an error, timed out, or runner failed | `plugin_resolve_failed` | Download failure; fallback may refresh once. |
| Resolve result malformed, extra, unsupported, or over 64 KiB | `plugin_resolve_invalid` | Download failure; fallback may refresh once. |
| Resolve `candidate_id` differs from selected `item_id` | `candidate_mismatch` | Download failure; fallback may refresh once; transport is not opened. |
| `ResolvedMedia` URL schema has controls, whitespace, credentials, fragment, explicit port, malformed syntax, or non-HTTPS scheme | `plugin_resolve_invalid` | Client rejects before transport/DNS; fallback may refresh once. |
| Transport receives a URL that fails its defense-in-depth parse/invariant check | `media_url_denied` | Download failure; no DNS or connection; fallback may refresh once. |
| Descriptor host is not an exact normalized manifest host | `media_host_denied` | Download failure; fallback may refresh once. |
| DNS answer is malformed, wrong family, private/non-global, or empty | `media_address_denied` for policy; `media_dns_failed` for resolution failure | Download failure; fallback may refresh once. |
| Numeric-IP connect fails or TLS negotiation fails | `media_connect_failed` or `media_tls_failed` | Download failure; fallback may refresh once. |
| DNS, connect, TLS, header, or body operation exceeds the remaining deadline | `media_timeout` | Download failure; fallback may refresh once; no raw timeout detail is exposed. |
| Redirect, non-2xx status, unsafe/duplicate headers, non-identity encoding, or MIME mismatch | `media_redirect_denied` or `media_response_invalid` | Download failure; fallback may refresh once. |
| Content length or observed bytes exceed 500 MiB | `file_too_large` | Download failure; fallback may refresh once. |
| Declared size differs from the observed byte count | `size_mismatch` | Download failure; fallback may refresh once. |
| Stream/read/temp-file/validation failure not covered above | `download_failed` (or the existing precise Phase 4 code) | Download failure; fallback may refresh once. |
| Any cancellation during resolve, open, stream, validation, or close | `download_cancelled` | Emit one failed event, finish close, re-raise; no fallback side effects. |
| Health operation timed out, errored, malformed, or was undeclared | `plugin_health_failed` from client, recorded as `health_failed` by fallback | `healthy=None`; never converts to a replacement download. A valid boolean `False` is returned as `False`. |
| Refresh errored or returned the failed source | `refresh_failed` or `refresh_included_failed_source` | Persist the failure outcome; no prompt if no valid rebind. |
| Durable reservation is missing, ambiguous, or fails path/hash/size verification | `artifact_uncertain` | Terminal job state; never choose a new suffix or repeat the source download. |
| `success_notice`, `selection_prompt`, or `terminal_failure_notice` completed with an acknowledged success | Matching notification effect is `done` | Mark only that effect complete; a duplicate replays the recorded result without sending again. |
| A WeCom send outcome is unknown after any attempt/error | `success_notice_uncertain`, `prompt_uncertain`, or `terminal_failure_notice_uncertain` | Durable terminal effect state; do not resend automatically; acknowledge the terminal job and require a new search/operator reconciliation. |

The implementation must add any new transport codes to the existing redacted error allowlist in `src/musicdl/media/models.py` and retain the current public Phase 4 codes. Tests assert exact code strings and cancellation behavior rather than matching arbitrary exception text.

## Task 3: Add typed plugin resolve and health helpers

**Files:**

- Modify: `src/musicdl/plugins/client.py` — add typed methods beside `invoke()` and reuse its bounded runner loop.
- Modify (tests): `tests/unit/test_plugin_client.py` — add resolve/health protocol, timeout, size, identity, and redaction tests to the existing client suite.

**Interfaces:**

- Consumes: `PluginClient.invoke(stored, request) -> PluginResponse`, `StoredPlugin`, `Candidate`, `PluginRequest`, and `ResolvedMedia`.
- Produces:

  ```python
  class PluginClient:
      async def resolve(
          self,
          stored: StoredPlugin,
          candidate: Candidate,
          *,
          timeout_ms: int | None = None,
      ) -> ResolvedMedia: ...

      async def health(
          self,
          stored: StoredPlugin,
          *,
          timeout_ms: int | None = None,
      ) -> bool: ...
  ```

  `resolve()` sends a `PluginRequest` with operation `resolve` and payload `{"candidate": candidate.public_representation}`, parses exactly one `ResolvedMedia`, and rejects a descriptor whose `candidate_id` differs from `candidate.item_id`. `health()` sends operation `health`, accepts only a boolean result, returns `False` only when the plugin returned a valid boolean false, and raises the stable `plugin_health_failed` error for timeout, protocol, undeclared-operation, or malformed responses. Both derive a bounded timeout from `timeout_ms` (or the client default), preserve the existing 64 KiB response and 1 MiB action limits, and map failures to the exact codes in the stable error table without logging URL credentials or response bodies. Neither method invokes compatibility `download` or handles media bytes.

- [ ] **Step 1: Write failing tests.** Add fake runner responses for valid resolve, candidate-ID mismatch, extra descriptor field, malformed/over-64 KiB response, timeout, a descriptor with URL syntax rejected by `ResolvedMedia`, health `True`/`False`, undeclared operation, and health timeout. Assert URL/schema failures become `plugin_resolve_invalid` before transport, valid health `False` is returned, while timeout/protocol/malformed health raises `RuntimeError("plugin_health_failed")`; captured requests contain descriptors only and never base64/media bytes.
- [ ] **Step 2: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_plugin_client.py -q
  ```

  Expected: FAIL because `resolve()` and `health()` are absent or untyped.
- [ ] **Step 3: Implement helpers.** Add a private timeout normalizer, construct UUID-backed `PluginRequest`s, call `invoke()`, map a failed resolve response/runner timeout to `plugin_resolve_failed`, map a malformed result to `plugin_resolve_invalid`, map identity mismatch to `candidate_mismatch`, validate `response.result` with `ResolvedMedia.model_validate`, and require `isinstance(result, bool)` for health. Map every health exception/invalid response to `plugin_health_failed`; return valid boolean `False` unchanged.
- [ ] **Step 4: Run GREEN.** Rerun the command; expected `N passed`, exit status 0, with no secret URL/query/body in exception or log assertions.
- [ ] **Step 5: Commit.**

  ```powershell
  git add src/musicdl/plugins/client.py tests/unit/test_plugin_client.py
  git commit -m "feat: add typed plugin resolve and health calls"
  ```

Keep the old `invoke()` and search/download compatibility behavior unchanged apart from shared bounds.

## Task 4: Complete `PluginSource` as the resolve-backed download adapter

**Files:**

- Modify: `src/musicdl/plugins/source.py` — retain the current normalized search adapter and add `download()`/`health()` using the client and transport.
- Modify (tests): `tests/unit/test_plugin_source.py` — add adapter and error-boundary tests to the existing source suite.

**Interfaces:**

- Consumes: existing `PluginSource.search(query: str) -> tuple[Candidate, ...]`, `PluginClient.resolve/health`, `SecureMediaTransport.open`, `StoredPlugin.manifest.allowed_hosts`, and `Candidate`.
- Produces:

  ```python
  class PluginSource:
      def __init__(
          self,
          stored: StoredPlugin,
          client: PluginClient,
          transport: SecureMediaTransport,
          *,
          resolve_stream_timeout_ms: int | None = None,
          health_timeout_ms: int | None = None,
      ): ...
      async def search(self, query: str) -> tuple[Candidate, ...]: ...
      async def download(self, candidate: Candidate) -> DownloadMetadata: ...
      async def health(self) -> bool: ...
  ```

  `download()` starts one monotonic `resolve_stream_timeout_ms` deadline, passes only the remaining milliseconds to `client.resolve(stored, candidate, timeout_ms=...)`, then passes the remaining milliseconds to `transport.open(descriptor, allowed_hosts=self.stored.manifest.allowed_hosts, timeout_ms=...)`; resolve and stream therefore share one deadline. It never calls plugin operation `download`. `health()` uses the constructor’s configured health timeout when calling typed client health and therefore raises a stable error for a plugin failure while returning `False` only for a valid boolean false response. Errors become the exact stable codes in the error table and retain source identity; no descriptor or response body is logged. The public adapter signatures remain the existing `search(query: str)`, `download(candidate)`, and `health()` protocol.

- [ ] **Step 1: Write failing tests.** Extend `test_plugin_source.py` with fake client/transport assertions: search behavior remains unchanged; valid `download(candidate)` resolves then opens main transport with `allowed_hosts=stored.manifest.allowed_hosts`; the fake records a single decreasing deadline across resolve and open; mismatched IDs fail before transport; a fake compatibility `download` operation raises if called; `health()` returns valid `False` but raises a stable error for timeout/protocol failure.
- [ ] **Step 2: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_plugin_source.py -q
  ```

  Expected: FAIL because `PluginSource` currently implements search only.
- [ ] **Step 3: Implement the adapter.** Add the required transport dependency and `resolve_stream_timeout_ms`/`health_timeout_ms` configuration to the constructor, validate the stored plugin/source identity as search already does, compute one monotonic deadline for resolve plus stream and pass remaining milliseconds to each method, and return the existing four-field `DownloadMetadata` stream.
- [ ] **Step 4: Run GREEN.** Rerun the command; expected `N passed`, exit status 0. Add an assertion that the compatibility operation call count is zero.
- [ ] **Step 5: Commit.**

  ```powershell
  git add src/musicdl/plugins/source.py tests/unit/test_plugin_source.py
  git commit -m "feat: resolve plugin media through main source adapter"
  ```

Do not change non-plugin providers or broaden `MusicSource` search semantics.

## Task 5: Make selection, job payloads, and fallback effects generation-safe

**Files:**

- Modify: `src/musicdl/wecom/state.py` — extend `SelectionContext`/`CONSUME_SCRIPT` and `RedisStateStore` to atomically place the immutable candidate JSON and selection generation in the job stream, and add stable job-effect markers.
- Modify: `src/musicdl/worker/selection.py` — preserve full candidate JSON in the request/user route and expose it to the consume path; retain hashed keys and atomic binding.
- Modify: `src/musicdl/worker/workers.py` — consume the immutable job candidate, pass configured sub-budgets, execute fence-guarded effects, replay durable outcomes, and handle acknowledged versus uncertain prompts without blind resend.
- Modify: `src/musicdl/media/fallback.py` — replace the hardcoded health timeout with `resolve_stream_timeout`, `refresh_timeout`, and `health_timeout` parameters while retaining one refresh and one health call.
- Modify (tests): `tests/unit/test_wecom_state.py` — add candidate snapshot/generation and effect-marker script cases.
- Modify (tests): `tests/unit/test_worker_selection.py` — add stale-token/generation tests.
- Modify (tests): `tests/unit/test_worker_message.py` — assert live-lease messages remain pending without retry or acknowledgement and that the explicit stream ID reaches the job handler.
- Modify (tests): `tests/unit/test_worker_job.py` — add immutable job payload, concurrency, and redelivery tests.
- Modify (tests): `tests/unit/test_media_fallback.py` — add configured health timeout coverage.

**Interfaces:**

- Consumes: `SelectionContext(corp_id, from_user, request_id, candidate_set_version, candidates, query, selection_generation)`, `Candidate`, `SearchResult`, and `download_with_fallback()`.
- Produces:

  ```python
  @dataclass(frozen=True)
  class SelectionContext:
      corp_id: str
      from_user: str
      request_id: str
      candidate_set_version: str  # SearchResult.version content hash
      candidates: dict[int, Candidate]  # index -> complete immutable Candidate snapshot
      query: str = ""
      selection_generation: int = 0

  @dataclass(frozen=True)
  class JobEffect:
      job_id: str
      effect: str
      status: str  # busy, running, done, uncertain
      stage: str  # claimed, external_started, completed, or uncertain
      owner: str | None
      fence: int
      lease_until_ms: int | None
      result: dict[str, Any] | None = None

  @dataclass(frozen=True)
  class EffectLease:
      job_id: str
      effect: str
      owner: str
      fence: int
      lease_until_ms: int
      stage: str

  class RedisStateStore:
      async def consume_selection(
          self,
          token: str,
          context: SelectionContext,
          index: int,
          *,
          ttl: int = 172800,
      ) -> JobResult: ...
      async def begin_job_effect(
          self,
          job_id: str,
          effect: str,
          owner: str,
          *,
          lease_ms: int,
          ttl: int,
      ) -> EffectLease | JobEffect: ...
      async def renew_job_effect(
          self,
          job_id: str,
          effect: str,
          owner: str,
          fence: int,
          *,
          lease_ms: int,
      ) -> EffectLease: ...
      async def begin_external_effect(
          self,
          job_id: str,
          effect: str,
          owner: str,
          fence: int,
          *,
          ttl: int,
      ) -> EffectLease: ...
      async def complete_job_effect(
          self,
          job_id: str,
          effect: str,
          owner: str,
          fence: int,
          result: dict[str, Any],
          *,
          ttl: int,
      ) -> JobEffect: ...
      async def mark_job_effect_uncertain(
          self,
          job_id: str,
          effect: str,
          owner: str,
          fence: int,
          code: str,
          *,
          ttl: int,
      ) -> JobEffect: ...
      async def get_job_effect(self, job_id: str, effect: str) -> JobEffect | None: ...
      async def prepare_artifact(
          self,
          job_id: str,
          candidate: Candidate,
          *,
          media_root: str | Path,
          base_relative_path: str,
          extension: str,
          media_type: str,
          declared_size: int | None,
          owner: str,
          fence: int,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def record_stream_complete(
          self,
          job_id: str,
          *,
          owner: str,
          fence: int,
          size_bytes: int,
          sha256: str,
          extension: str,
          media_type: str,
          declared_size: int | None,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def mark_artifact_published(
          self,
          job_id: str,
          *,
          owner: str,
          fence: int,
          ttl: int,
      ) -> ArtifactRecord: ...
      async def get_artifact(self, job_id: str) -> ArtifactRecord | None: ...

  class JobWorker:
      async def handle_job(self, job: Mapping[str, Any], *, job_id: str) -> FallbackResult: ...
      async def run_once(self) -> int: ...

  async def download_with_fallback(
      candidate: Candidate,
      sources: Mapping[str, DownloadSource],
      media_root: str | Path,
      *,
      request_id: str,
      query: str,
      refresh: Callable[[str, frozenset[str]], Awaitable[SearchResult]],
      resolve_stream_timeout: float | None = None,
      refresh_timeout: float | None = None,
      health_timeout: float = 10.0,
      reservation: ArtifactRecord | None = None,
      language: str | None = None,
      max_bytes: int = MAX_MEDIA_BYTES,
      record: Callable[[DownloadEvent], None] | None = None,
  ) -> FallbackResult: ...
  ```

  `candidate_set_version` is the existing content hash from `SearchResult.version`; it is never a generation counter. `issue_selection()` and `bind_user_selection()` validate every complete `Candidate`, serialize the canonical `model_dump(mode="json")` snapshot into the token, and atomically write the user/request indexes to that same token. `query` and integer `selection_generation` are stored beside the snapshot. `consume_selection()` validates the token/context identity, version, generation, and index, then `CONSUME_SCRIPT` atomically XADDs the full fields `candidate` (canonical JSON), `corp_id`, `from_user`, `query`, `request_id`, `version` (the content hash), `generation` (the integer), and `index`; it also persists the same immutable record at `{namespace}:job:{stream_id}` before deleting the consumed token. The Redis stream ID returned by XADD is the stable `job_id`. A stale generation or altered snapshot is rejected, and an already-consumed token returns the original job ID only when every identity/version/generation/index field matches.

  `JobWorker.handle_job(job, *, job_id: str)` requires the Redis stream ID explicitly. `run_once()` calls `handle_job(job, job_id=str(message_id))`; it never lets a mutable request or user route replace an already-XADDed candidate. A legacy payload without `candidate` may be accepted only by taking and freezing a route snapshot before claiming any effect, then treating that snapshot as immutable for the rest of the job. New jobs always use the full stream payload and never perform a route lookup.

  Effect state is stored under namespaced job/effect keys as a durable fenced state machine. Lua transitions use Redis `TIME`, an owner token, a monotonically increasing fence, an absolute lease deadline, a stage, and a redacted result; lease expiry is decided in Redis rather than by a worker clock. `begin_job_effect()` atomically creates or claims an unstarted stage and returns either an owner/fence lease, a completed/uncertain record, or a live-lease `busy` result. `renew_job_effect()` extends only the matching owner/fence lease. `begin_external_effect()` is a compare-and-set transition to `external_started` immediately before a network or WeCom side effect. Artifact publication uses the owner/fence transitions `stream_complete -> publishing -> physically_published -> published`; a verifiable exact target can therefore be replayed safely without a source call. `complete_job_effect()` is a compare-and-set on job ID, effect, owner, fence, and running stage; stale fences cannot commit. If a lease expires while `external_started`, the next worker records terminal `uncertain` and does not replay the external call unless the deterministic reservation already contains a verifiable published artifact; in that case it verifies the exact persisted target and completes the durable result without the source call. If a lease expires before the external stage, a higher fence may resume from the last durable stage subject to the artifact takeover matrix. Retain terminal records for the configured TTL and make reads replayable.

  Derive each lease from the one handler deadline: set `owner = f"{consumer}:{uuid4().hex}"`, allocate the next fence independently for each `{job_id,effect}`, and use `lease_ms = min(pending_idle_ms - 1000, ceil((max(deadline - monotonic_now, 0) + redis_overhead_seconds) * 1000))` with a minimum of 1000 ms. `redis_overhead_seconds` is a fixed 1-second allowance charged to the same deadline; the configuration validator requires `pending_idle_ms > 1000 * (max(search_timeout, job_timeout) + redis_overhead_seconds)`, so the default 32000 ms is beyond the 31000 ms initial lease. Before every external call, the worker spends no new budget: it renews the matching owner/fence lease, immediately performs `begin_external_effect()` and verifies the returned owner, fence, and stage, then invokes the call only if the remaining handler deadline is positive. Every Redis read/CAS/renew operation is bounded by the remaining deadline. `handle_job()` starts one deadline before its first Redis operation and wraps artifact work, media resolve/stream, fallback refresh/health, and all WeCom notices in `asyncio.timeout_at(deadline)`; each `send_text` is capped at 10 seconds or the remaining deadline, whichever is smaller. A deadline/cancellation after an external attempt records the corresponding uncertain state when possible and never extends or blindly replays the handler.

  A live-lease `busy` result is in progress, not a business failure: the redelivered message receives no retry increment, no dead-letter record, and no XACK, so the current owner can finish or its lease can expire. A `done` result is replayed without another external call. An `uncertain` result is recorded as terminal, acknowledged once, and surfaced with its stable code for operator/user reconciliation. This rule applies independently to `download`, `refresh`, `health`, `rebind`, `success_notice`, `selection_prompt`, and `terminal_failure_notice` effects; notification effect keys are never collapsed into the media effect key.

  The owner creates the deterministic `ArtifactRecord` from `job_id` and passes it to `download_with_fallback(..., resolve_stream_timeout=self.resolve_stream_timeout, refresh_timeout=self.refresh_timeout, health_timeout=self.health_timeout, reservation=reservation)` through state-backed wrappers that claim and complete the corresponding effect before and after each source download, refresh, and health call. After a terminal media failure, if `result.refreshed` is present it creates a new `SelectionContext` using `result.refreshed.version` as the content hash and `old_selection_generation + 1` as the integer generation, plus the actual refreshed `Candidate.model_dump(mode="json")` values. It claims `rebind`, calls `issue_selection()` and `bind_user_selection()`, and records the resulting token/context before claiming the separate `selection_prompt` notification effect. A successful download claims `success_notice`; a terminal retry/dead-letter path claims `terminal_failure_notice`. Each notification call uses its own owner/fence CAS and a 10-second maximum WeCom timeout. A known acknowledgement completes the effect, while an exception/unknown outcome records the matching uncertain terminal (`prompt_uncertain` for `selection_prompt`, `success_notice_uncertain`, or `terminal_failure_notice_uncertain`), never resends automatically, and still ACKs the terminal job. The selection prompt body is exactly `format_results(...) + "\n\n回复序号下载。"`. Old tokens have a different or consumed generation and are rejected; no refreshed candidate is auto-downloaded.

- [ ] **Step 1: Write failing state tests.** Extend `test_wecom_state.py` to assert the consume Lua call receives a canonical full candidate snapshot and separate version/generation fields, persists `{namespace}:job:{stream_id}`, rejects malformed snapshots and stale generations, and returns the original job ID only for an identical duplicate consume. Add fake-Redis script tests using `TIME` for: first `begin_job_effect()` returning owner/fence, a live lease returning `busy`, renewal requiring the same owner/fence, `begin_external_effect()` marking the external stage, completion rejecting a stale fence, and an expired external stage becoming `uncertain` rather than being claimed. Add `ArtifactStore` tests for path validation under `media_root`, symlink parents, exclusive `0600`/no-follow `.musicdl-staging/<sha256(job_id)[:40]>.<fence>.part`, preexisting base and `(2)` occupancy with holes, distinct concurrent same-title suffix slots, repeated/idempotent prepare returning the same slot and exact paths, the per-base fenced lock and Redis slot->job/job->slot ledger CAS, safe pre-publication cleanup release only, `takeover_artifact()` actions for every state and live-owner conflict, strict higher-fence enforcement, stale-owner mutation/delete/publish rejection, owner/fence CAS rejection for `record_stream_complete()`, `claim_artifact_publish()`, and `mark_artifact_published()`, and persistence of only relative paths/checkpoint fields.
- [ ] **Step 2: Write failing worker/fallback/message tests.** Add tests for two deliveries of the same job, two concurrent `handle_job()` calls, old token after refresh, selected candidate surviving request-route replacement, all full stream fields, explicit `job_id` reaching `handle_job`, one refresh with exactly `frozenset({"failed"})`, one health call, one selection prompt, and zero replacement download calls. Add a live-lease duplicate assertion that retry count and XACK remain unchanged, stale-fence commit rejection, and the crash/retention boundaries: after `prepare_artifact` before `external_started` (same reservation resumes only when temp/target are absent), after expiry in `prepared + external_started` (terminal `artifact_uncertain`, no source replay), after `record_stream_complete` before publication (higher-fence takeover rehashes the exact temp and publishes without a source call), after `claim_artifact_publish`/physical publication before the next CAS (the exact reserved target is verified and publication is completed), and after `published` before job completion (the exact target is verified and the durable result is replayed). Assert partial temp files, unexpected occupied targets, symlink parents, or exact-target hash/size/MIME/candidate mismatches become `artifact_uncertain` and never publish, delete another owner’s file, scan suffixes, or redownload. Exercise known and uncertain `success_notice`, `selection_prompt`, and `terminal_failure_notice`; an uncertain send ACKs without resending. Add `resolve_stream_timeout`, `refresh_timeout`, and `health_timeout` boundaries to `test_media_fallback.py` and modify `test_worker_message.py` for the busy path.
- [ ] **Step 3: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_wecom_state.py tests/unit/test_worker_selection.py tests/unit/test_worker_message.py tests/unit/test_worker_job.py tests/unit/test_media_fallback.py -q
  ```

  Expected: FAIL because jobs currently carry only ID/index/request ID, fallback has no configured sub-budgets, retry expiry is hardcoded, route lookup is mutable, and no fenced effect transitions exist.
- [ ] **Step 4: Implement the atomic selection and fenced state machine.** Change selection storage to canonical full `Candidate` JSON plus query/version/generation and make the Lua consume script XADD every required job field and persist `{namespace}:job:{stream_id}` atomically. Validate snapshots before the script and preserve identical duplicate `JobResult` semantics. Add Redis `TIME`-based Lua transitions for `begin_job_effect`, `renew_job_effect`, `begin_external_effect`, `complete_job_effect`, `mark_job_effect_uncertain`, and `get_job_effect`; compare owner and fence on every mutation, retain redacted results, return `busy` for a live lease, and convert expired `external_started` stages to `uncertain` without replaying them. Add the owner/fence CAS `prepare_artifact`, `record_stream_complete`, `takeover_artifact`, `claim_artifact_publish`, `mark_artifact_published`, and `get_artifact` methods for `ArtifactRecord`; `takeover_artifact(new_owner, new_fence, now_ms, lease_ms)` requires a strictly higher fence and returns `resume`, `uncertain`, `verify_publish`, `verify_complete`, `replay`, or live-lease `owner_conflict` according to the exact matrix. Persist only POSIX-relative paths and checkpoint fields, never an absolute media-root path.
- [ ] **Step 5: Implement worker orchestration and artifact replay.** Add `resolve_stream_timeout`, `refresh_timeout`, and `health_timeout` to `JobWorker`; require `handle_job(job, *, job_id: str)` and pass the stream ID from `run_once()`. Claim the stable job effects before `download`, `refresh`, `health`, and `rebind`, and claim the separate `success_notice`, `selection_prompt`, and `terminal_failure_notice` effects before WeCom calls. Pass the configured sub-budgets, freeze the candidate from the stream, and leave live-lease messages pending without retry or XACK. Before the source call, derive the base once and acquire a short fenced per-base allocation lock; reconcile the Redis slot->job/job->slot ledger and exact filesystem occupancy (including preexisting slots and sidecars), then atomically persist the selected slot and exact temp/target paths in the artifact record. Repeated prepare returns the exact existing record, and redelivery never chooses a new suffix, scans siblings, or infers ownership. Reserve target/temp paths deterministically from the stream job ID before the source call, persist candidate identity, expected size/hash, extension/MIME, owner/fence, and state, then apply `takeover_artifact()` and replay only the exact persisted artifact: `prepared` resumes only before `external_started` with temp/target absent; expired `external_started` is uncertain; `stream_complete` rehashes the exact temp and publishes without a source call; `publishing`/`physically_published` verify the exact reserved target and CAS-complete; `published` verifies and replays. A missing, unexpected, occupied-but-mismatched, or ambiguous exact path returns `artifact_uncertain` without a second source download. Never publish a partial temp: require EOF, `fsync`, close, hash, size, signature, and MIME checkpoint followed by `record_stream_complete` before `claim_artifact_publish` and exclusive publication, then `fsync` file/parent and `mark_artifact_published`; all allocation, cleanup, and publication mutations reject stale owners/fences. The acknowledged notification completes its own effect; an unknown send outcome records the matching uncertain terminal and ACKs without resending. Replace `_record_failure()`’s fixed `604800` retry-key expiry with the configured `retry_window_seconds`. Preserve cancellation, retries, dead-letter handling, atomic Phase 4 normalization/validation/publication, and existing WeCom text behavior.
- [ ] **Step 6: Run GREEN.** Rerun the command; expected `N passed`, exit status 0. Specifically assert that the old route cannot alter an already-XADDed job candidate, stale fences cannot complete, a live duplicate does not increment retries or XACK, preexisting base/`(2)` files and holes yield the lowest durable unclaimed slot, concurrent jobs receive distinct slots, repeated reservation returns the same exact slot/path, each artifact crash boundary and retained temp/target state replays safely without a duplicate source call, partial or symlinked paths never publish, occupied-but-mismatched exact targets become uncertain, no suffix scan or inferred ownership occurs, expired external stages become terminal uncertain, and redelivery does not call download, refresh, health, bind, or any notification effect twice.
- [ ] **Step 7: Commit.**

  ```powershell
  git add src/musicdl/wecom/state.py src/musicdl/worker/selection.py src/musicdl/worker/workers.py src/musicdl/media/fallback.py tests/unit/test_wecom_state.py tests/unit/test_worker_selection.py tests/unit/test_worker_message.py tests/unit/test_worker_job.py tests/unit/test_media_fallback.py
  git commit -m "fix: make selection fallback generation safe"
  ```

If state schema compatibility is needed, retain a reader for old records and fail closed when generation/candidate identity is ambiguous; do not delete live state fields.

## Task 6: Validate timeout budgets and assemble real plugin sources

**Files:**

- Modify: `src/musicdl/config.py` — add a `WorkerSettings` model and cross-field budget validation under `AppSettings`.
- Modify: `src/musicdl/app.py` — update `_Runtime`, `_build_runtime()`, lifecycle close order, shared transport/client construction, worker source map, and configured timeouts.
- Modify (tests): `tests/unit/test_config.py` — add worker budget boundary tests.
- Modify (tests): `tests/unit/test_runtime_assembly.py` — update fakes and assertions for shared dependencies and resolve-capable sources.

**Interfaces:**

- Consumes: existing `AppSettings`, `PluginSettings`, `PluginStore.enabled()`, `PluginSource`, `SourceEntry`, `MessageWorker`, `JobWorker`, and `HttpsActionBroker`.
- Produces:

  ```python
  REDIS_OVERHEAD_SECONDS = 1.0
  WECOM_NOTICE_TIMEOUT_SECONDS = 10.0

  class WorkerSettings(BaseModel):
      search_timeout: float = Field(default=8.0, gt=0, le=30)
      resolve_stream_timeout: float = Field(default=15.0, gt=0, le=30)
      health_timeout: float = Field(default=5.0, gt=0, le=30)
      job_timeout: float = Field(default=30.0, gt=0, le=30)
      budget_slack_seconds: float = Field(default=2.0, gt=0, le=30)
      pending_idle_ms: int = Field(default=32000, ge=1, le=604800000)
      job_ttl: int = Field(default=172800, ge=60, le=604800)
      retry_window_seconds: int = Field(default=86400, ge=1, le=604800)
      max_attempts: int = Field(default=3, ge=1, le=100)

  class AppSettings(BaseSettings):
      # retain the existing nested fields, adding this one beside media/wecom/plugin
      worker: WorkerSettings = WorkerSettings()

  def _build_runtime(settings: AppSettings, clock=None) -> _Runtime: ...
  ```

All duration fields are seconds except `pending_idle_ms`, which is milliseconds. The validator rejects `pending_idle_ms <= 1000 * (max(search_timeout, job_timeout) + REDIS_OVERHEAD_SECONDS)`, `health_timeout > job_timeout`, `resolve_stream_timeout + search_timeout + health_timeout >= job_timeout`, or `job_timeout - (resolve_stream_timeout + search_timeout + health_timeout) < budget_slack_seconds`, `retry_window_seconds <= max_attempts * ceil(pending_idle_ms / 1000)`, and `job_ttl <= retry_window_seconds + ceil(job_timeout)`. The defaults are `8 + 15 + 5 = 28` seconds inside a 30-second job budget with 2 seconds of material slack, `32000 > 1000 * (max(8, 30) + 1)`, `86400 > 3 * ceil(32000 / 1000)`, and `172800 > 86400 + ceil(30)`. `refresh_timeout` is derived from the same configured `search_timeout` when the worker calls fallback, so refresh cannot consume an unbudgeted extra window. The full handler deadline also includes Redis overhead, artifact work, and up to `WECOM_NOTICE_TIMEOUT_SECONDS` for each bounded WeCom notice; it is never extended by a lease renewal. Pass `retry_window_seconds` into the worker so the retry key no longer uses a fixed seven-day expiry. `_build_runtime()` constructs exactly one `SecureMediaTransport` and one `PluginClient`, passes the transport to every enabled search+resolve `PluginSource`, and gives `JobWorker` a non-empty `{source_id: source}` map for resolve-capable plugins. Use configured values in both workers; remove the empty source map and app-level hardcoded timeout values. `_Runtime.aclose()` closes transport, plugin client, then Redis once.

- [ ] **Step 1: Write failing config/runtime tests.** Extend `test_config.py` for every invalid equality/under-bound and valid boundary: the 8/15/5/30 defaults, material 2-second slack, `pending_idle_ms` equality and one-millisecond-under cases for the Redis-overhead-adjusted bound, seconds-to-milliseconds conversion, `retry_window_seconds` equality and one-second-under cases, and `job_ttl` equality/under cases. Update the `test_runtime_assembly.py` fakes with `worker` settings, assert one shared transport/client identity, assert only enabled search plugins register, assert only plugins declaring both `search` and `resolve` enter `job_worker.sources`, assert `refresh_timeout` receives the configured search budget, and assert a hanging notice is capped at 10 seconds and at the remaining handler deadline.
- [ ] **Step 2: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_runtime_assembly.py -q
  ```

  Expected: FAIL because `AppSettings` has no worker budget model and `_build_runtime()` currently passes an empty source map and app-level hardcoded timeout values.
- [ ] **Step 3: Implement configuration.** Add `WorkerSettings` to `AppSettings` with the exact defaults above, reject booleans/non-finite values through strict validators where the existing settings do so, and enforce every cross-field inequality in one Pydantic model validator. Keep secrets excluded from repr/dumps.
- [ ] **Step 4: Implement runtime wiring.** Add a transport field to `_Runtime`, construct it once, pass it into `PluginSource` with `resolve_stream_timeout_ms=ceil(settings.worker.resolve_stream_timeout * 1000)` and `health_timeout_ms=ceil(settings.worker.health_timeout * 1000)`, derive `source_map` from entries whose manifest operations include both `search` and `resolve`, and forward worker settings to `MessageWorker`/`JobWorker` including `search_timeout`, `resolve_stream_timeout`, `refresh_timeout=settings.worker.search_timeout`, `health_timeout`, `job_timeout`, `pending_idle_ms`, `job_ttl`, `retry_window_seconds`, and `max_attempts`.
- [ ] **Step 5: Run GREEN.** Rerun the command; expected `N passed`, exit status 0. Assert the configured `pending_idle_ms` exceeds the maximum handler timeout plus the Redis-overhead allowance and every derived effect lease, the full handler timeout includes bounded WeCom delivery, and lifecycle close order is deterministic.
- [ ] **Step 6: Commit.**

  ```powershell
  git add src/musicdl/config.py src/musicdl/app.py tests/unit/test_config.py tests/unit/test_runtime_assembly.py
  git commit -m "feat: wire plugin sources and validated worker budgets"
  ```

Do not alter Telegram, AI, Compose, or deployment configuration in this task.

## Task 7: Add deterministic in-process vertical-slice fixtures

**Files:**

- Create: `tests/fixtures/plugins/vertical_primary.py` — deterministic `handle(request)` search/resolve/health fixture; compatibility `download` raises if called.
- Create: `tests/fixtures/plugins/vertical_backup.py` — deterministic backup search/resolve/health fixture.
- Create (tests): `tests/integration/test_plugin_download_vertical_slice.py` — in-process runner/client/transport/runtime success and failure scenarios; this is separate from the existing Docker-only `tests/integration/test_plugin_runtime.py`.

**Interfaces:**

- Consumes: `PluginSource`, `PluginClient`, `SecureMediaTransport`, `download_candidate`, `download_with_fallback`, `RedisStateStore` fakes, and the existing Phase 4 validation/archive and WeCom seams.
- Produces deterministic fixtures whose `handle(request)` returns candidate JSON for `search`, a five-field descriptor for `resolve`, a boolean for `health`, and never media bytes in a runner response.

```python
def handle(request):
    operation = request["operation"]
    if operation == "search":
        return [{"source_id": "primary", "source_version": "1", "item_id": "primary-1",
                 "title": "Song", "artist": "Artist", "format": "mp3"}]
    if operation == "resolve":
        candidate = request["payload"]["candidate"]
        return {"candidate_id": candidate["item_id"], "url": "https://media.example/song.mp3",
                "extension": "mp3", "media_type": "audio/mpeg", "declared_size": 13}
    if operation == "health":
        return True
    if operation == "download":
        raise RuntimeError("compatibility operation must not be called")
    raise ValueError("unsupported operation")
```

- [ ] **Step 1: Write failing integration tests.** In the success case, assert search -> selection token -> primary resolve -> fake main transport bytes -> Phase 4 archive -> WeCom success text. In the failure case, make primary resolve/stream fail and assert one excluded-source refresh, one primary health call, actual backup candidates persisted under a new version, one new WeCom prompt, and zero backup download calls. Assert runner request/response capture contains no media bytes/base64/credentials.
- [ ] **Step 2: Run RED.** Run:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/integration/test_plugin_download_vertical_slice.py -q
  ```

  Expected: FAIL because the runtime source map, typed operations, immutable job payload, and main transport are not yet wired.
- [ ] **Step 3: Add only deterministic fakes.** Keep network disabled; fake DNS/socket/transport and Redis/WeCom state at process boundaries. Do not weaken production validation to satisfy a fixture.
- [ ] **Step 4: Run GREEN.** Rerun the command; expected `N passed`, exit status 0. Then run the focused suite:

  ```powershell
  .venv\Scripts\python.exe -m pytest tests/unit/test_plugin_contract.py tests/unit/test_media_transport.py tests/unit/test_media_download.py tests/unit/test_plugin_client.py tests/unit/test_plugin_source.py tests/unit/test_wecom_state.py tests/unit/test_worker_selection.py tests/unit/test_worker_message.py tests/unit/test_worker_job.py tests/unit/test_media_fallback.py tests/unit/test_config.py tests/unit/test_runtime_assembly.py tests/integration/test_plugin_download_vertical_slice.py -q
  ```

  Record the completed result and exit status.
- [ ] **Step 5: Commit.**

  ```powershell
  git add tests/fixtures/plugins/vertical_primary.py tests/fixtures/plugins/vertical_backup.py tests/integration/test_plugin_download_vertical_slice.py
  git commit -m "test: cover plugin download vertical slice"
  ```

If the integration harness cannot run in the environment, keep equivalent unit fake-boundary assertions and report the integration gap; do not add real-network dependencies.

## Task 8: Document boundaries and perform final acceptance review

**Files:**

- Modify: `README.md` — document plugin operation boundaries, descriptor fields, limits, secure transport ownership, generation-bound selection, and failure/rebind behavior.
- Modify: `docs/superpowers/specs/2026-09-12-phase-6-plugin-runtime-design.md` — correct only stale statements about plugin media bytes, operation ownership, or empty worker sources.
- Modify: `docs/superpowers/specs/2026-09-12-phase-4-download-design.md` — correct only the metadata close/stream ownership note if required by the implementation.

**Interfaces:**

- Consumes: settled code and the two approved design specs.
- Produces: concise operator/developer documentation stating that plugins search and resolve descriptors, the main process streams, validates signatures/extension/MIME, atomically publishes the media file, and only then allows the worker to XACK; `download` is compatibility-only and failure prompts a user-selected retry.

- [ ] **Step 1: Write the documentation check.** Search the three existing documents for claims about media bytes, empty source maps, hardcoded timeouts, redirects, health, and candidate routes; record stale locations before editing.
- [ ] **Step 2: Make minimal documentation edits.** Include exact 64 KiB/1 MiB/500 MiB bounds, HTTPS/IDNA/DNS controls, no credentials/redirects/proxies, close ownership, timeout inequalities, one-time generation, fence-guarded refresh/health/rebind transitions, separate `success_notice`, `selection_prompt`, and `terminal_failure_notice` effects, acknowledged-notification replay, effect-specific uncertain terminals including `prompt_uncertain` without resend, no automatic replacement, unknown-language archive behavior, atomic Phase 4 publication, and XACK only after the recorded effect completes. Do not describe deferred providers or classifiers as delivered.
- [ ] **Step 3: Run final tests.** Use the focused command from Task 7, then run the exact repository-wide command `.venv\Scripts\python.exe -m pytest -q`, followed by the strict warning gate `.venv\Scripts\python.exe -m pytest -q -W error`; capture each completed result and exit status. A started process is not a pass.
- [ ] **Step 4: Run static scope checks after all edits settle.** Run:

  ```powershell
  git diff --check
  $forbidden = @('T'+'BD', 'TO'+'DO', 'implement '+'later', 'fill '+'in', 'Similar '+'to', 'write tests '+'for the above')
  rg -n ($forbidden -join '|') docs/superpowers/plans/2026-09-14-plugin-download-vertical-slice.md
  ```

  Expected: `git diff --check` exits 0 and the placeholder scan prints no lines (exit 1 for no matches). Review the complete diff and `git diff --stat`; changed paths must be limited to the task file map.
- [ ] **Step 5: Commit docs.**

  ```powershell
  git add README.md docs/superpowers/specs/2026-09-12-phase-6-plugin-runtime-design.md docs/superpowers/specs/2026-09-12-phase-4-download-design.md
  git commit -m "docs: document plugin download boundaries"
  ```

Sol must independently review the real diff, security/concurrency evidence, path scope, and completed test exits before acceptance. Only after acceptance may the coordinator push the current branch to `origin` (`Gaoshou101/musicdl`); never force-push or push failed/unaccepted work.

## Final acceptance checklist

- [ ] `ResolvedMedia` is frozen, forbids extras, validates URL controls and extension/MIME/size, and is bound to the selected `Candidate.item_id`.
- [ ] Existing `DownloadMetadata` still carries `chunks`, `extension`, `media_type`, and `declared_size`, and its response closer runs exactly once on success/error/cancellation.
- [ ] Plugin response/action/media bounds remain 64 KiB/1 MiB/500 MiB; runner JSON contains no media bytes or base64.
- [ ] Main transport enforces exact IDNA host policy, global DNS answers, numeric-IP connect with hostname SNI/`Host`, no proxy/redirect/ambient credentials, bounded response, and closure.
- [ ] `PluginClient.resolve()`/`.health()` are typed, timed, bounded, redacted, and `PluginSource.download()` never calls compatibility `download`.
- [ ] `_build_runtime()` in `src/musicdl/app.py` injects one transport/client and a non-empty resolve-capable source map; its worker timeouts come from `AppSettings.worker` rather than app-level hardcoded values.
- [ ] Selection jobs contain immutable candidate/generation data; stale tokens cannot select refreshed candidates; stable job-effect markers suppress duplicate download, refresh, health, rebind, and prompt side effects.
- [ ] Artifact records use owner/fence CAS `prepared -> stream_complete -> published`, persist only safe POSIX-relative paths, never publish a partial temp, and replay only a uniquely verified path/hash/size/MIME match; concurrent same-title jobs receive distinct durable suffix reservations.
- [ ] Notification effects are separate (`success_notice`, `selection_prompt`, `terminal_failure_notice`); acknowledged outcomes replay durably, uncertain outcomes are terminal and never resent.
- [ ] Failure refresh excludes exactly the failed source, health-checks it once, persists actual refreshed candidates, sends one new WeCom prompt, and never auto-downloads a replacement.
- [ ] Phase 4 validation/archive and unknown-language behavior remain intact.

## Deferred scope

Telegram provider wiring, AI language classification, admin UI, CI/deployment, Docker/Compose changes, real provider plugins, real-network acceptance, redirects, range requests, retries, transcoding, and automatic replacement selection remain separate work requiring their own design and approval.
