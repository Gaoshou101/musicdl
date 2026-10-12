# Performance and artifact measurement baseline

These scripts provide matched, reproducible measurements for source search,
media processing, admission waiting, static panel assets, and the two Docker
images. They do not make code changes or build images. Use the same Python
version, host, filesystem, resource limits, source fixture settings, and Docker
engine for baseline and candidate snapshots. Pass both `--source-commit` and
`--snapshot`; the latter distinguishes exported trees when `.git` is absent.
The scripts emit JSON Lines so each run can be retained and compared without
rounding away the raw measurements.

## Controlled application paths

Run from the repository root with Python 3.12 and the project source available
on `PYTHONPATH`. The commands below use 4 MiB media streams, twenty rounds per
media concurrency, a fixed seed, four in-memory search sources, and a bounded
admission queue. Set the revision variables and source directories to the two
snapshots under review.

```sh
BASELINE_SHA=3f6df2bb15cef1d8a5a6c5d3e8cd961582654f91
CANDIDATE_SHA=replace-with-candidate-revision
RUN_DIR=/dev/shm/musicdl-perf-20261012-01
mkdir -p "$RUN_DIR/tmp"

TMPDIR="$RUN_DIR/tmp" PYTHONPATH=/tmp/musicdl-baseline/src \
  python scripts/performance/benchmark_search_admission.py \
  --source-commit "$BASELINE_SHA" --snapshot baseline \
  > "$RUN_DIR/search-admission-baseline.jsonl"

TMPDIR="$RUN_DIR/tmp" PYTHONPATH=/tmp/musicdl-candidate/src \
  python scripts/performance/benchmark_search_admission.py \
  --source-commit "$CANDIDATE_SHA" --snapshot candidate \
  > "$RUN_DIR/search-admission-candidate.jsonl"

TMPDIR="$RUN_DIR/tmp" PYTHONPATH=/tmp/musicdl-baseline/src \
  python scripts/performance/benchmark_media_download.py \
  --source-commit "$BASELINE_SHA" --snapshot baseline \
  --size-mib 4 --rounds 20 --concurrency 1,4,8,16 --seed 1296671049 \
  > "$RUN_DIR/media-baseline.jsonl"

TMPDIR="$RUN_DIR/tmp" PYTHONPATH=/tmp/musicdl-candidate/src \
  python scripts/performance/benchmark_media_download.py \
  --source-commit "$CANDIDATE_SHA" --snapshot candidate \
  --size-mib 4 --rounds 20 --concurrency 1,4,8,16 --seed 1296671049 \
  > "$RUN_DIR/media-candidate.jsonl"
```

`benchmark_search_admission.py` measures the real `search_sources()`
orchestration against deterministic in-memory `SourceRegistry` adapters. Its
search p50/p95 is the time for that orchestration, including validation,
deduplication, and ordering. It includes no provider or network latency. The
script separately measures `DownloadAdmission` global-slot and per-source
queue wait with held permits; `--hold-ms` (default 25) creates the controlled
queue condition. Queue wait is not an end-to-end download duration. The
synthetic providers yield once to the event loop and do not sleep to imitate a
remote service.

`benchmark_media_download.py` invokes the actual `download_candidate()` path
with deterministic parser-shaped MP3 bytes. It reports per-download latency,
throughput in MiB/s, event-loop lag in seconds, peak process RSS in bytes, and
thread count. The largest configured concurrency times stream size must remain
within the 64 MiB temporary-file budget. RSS is the process high-water mark
reported by the platform API, so compare runs from the same process shape and
host. This workload excludes real source resolution, network latency, and
production request overhead.

`download_stage_seconds` contains the real journal's `resolve` and `transfer`
span p50/p95 values in seconds and the number of samples for each stage. The
benchmark uses an in-memory journal sink, so it does not include SQLite write
cost. Spans can overlap and must not be added to reconstruct total latency.

Both commands include Python/platform, implementation path where applicable,
workload settings, and source identity in their metadata row. Preserve the JSONL
files outside the checkout. Do not describe a single matched run as a general
production throughput forecast; repeat runs on a stable host before drawing a
performance conclusion.

## Static assets and Docker images

Use assets already copied from the built main image and images already built by
CI. This command recursively reports each `.js` and `.css` file's raw bytes and
gzip bytes, their totals, and both Docker image inspect records:

```sh
RUN_DIR=/tmp/musicdl-artifacts
mkdir -p "$RUN_DIR"
python scripts/performance/measure_artifacts.py \
  --assets-dir /tmp/musicdl-panel \
  --image musicdl:ci --image musicdl-plugin-runner:ci \
  --source-commit "$CANDIDATE_SHA" --snapshot candidate \
  > "$RUN_DIR/artifacts-candidate.jsonl"
```

For a running main container, extract the panel before measuring it with
`docker cp "$PANEL_CONTAINER":/app/panel /tmp/musicdl-panel`. The `.js` and `.css`
files are compressed individually with gzip level 9 and `mtime=0`; JSON output
records Python and zlib versions. Docker `Size` is the image's local
uncompressed size in bytes. It is not the compressed registry transfer size,
and each image size includes its own layer accounting. The script only inspects
the supplied directory and images; it does not build, start, or mutate them.

Compare baseline and candidate JSONL using the same asset extraction path and
Docker engine. Keep source commit, image ID/digests, asset file count, raw/gzip
byte totals, and environment metadata together so the reported numbers retain
their provenance. These measurements do not replace a browser workflow smoke
check for full UI behavior.

## Authenticated browser regression

The `admin-panel` CI job exports the actual panel from its built deployment
image and runs `cd web && npm run smoke:browser` against
`tests/browser/fixture_app.py`. Set `MUSICDL_BROWSER_PANEL_ROOT` to that export
and optionally `MUSICDL_BROWSER_ARTIFACTS` to the output directory. Run this on
the project test server or hosted CI with Python 3.12, the project installed,
Node 22, the locked web dependencies, and Playwright Chromium installed.

The fixture uses temporary stores and a loopback server with a deterministic
source. Authentication, cookie restoration after reload, CSRF, source-list
preview/install, search, download, and persisted history use real application
routes. Test-only controls hold the source's resolve and transfer stages until
the browser has observed each running-stage message, then release the download
to completion. Only the final observability failure is intercepted: a 503 must
remove the previously displayed counters. This checks UI integration and does
not establish external-provider availability or production download speed.

CI retains the completed-detail screenshot and, on failure, a screenshot and
trace. The lite source-image job measures its already-built main and runner
images and their exported panel assets; JSONL reports identify the checked-out
commit, image IDs, byte totals, and measurement environment. Local builds can
have no registry digest; the image ID remains recorded. No extra image build
is required for these reports.

## Initial execution evidence (2026-10-12)

On the isolated Linux test server, Python 3.12.15 completed paired baseline
(`3f6df2bb15cef1d8a5a6c5d3e8cd961582654f91`) and candidate-working-tree runs
with the same corrected measurement scripts. Search/admission and all media
concurrencies exited successfully. The 128-result search boundary also returned
128 unique candidates with all four source statuses `ok`.

For 4 MiB streams, 20 rounds, concurrency 16, and seed `1296671049`, each media
run recorded 320 resolve and 320 transfer samples. Transfer p95 was 199.145 ms
for baseline and 187.360 ms for candidate; peak RSS was 52,174,848 and
52,224,000 bytes respectively. These single-run differences are not evidence
of a speedup: the search and download algorithms are unchanged. The controlled
25 ms admission hold produced approximately 25.6 ms waits.

The remote backend suite completed with 2,128 passed and 82 skipped; the
isolated real-Redis/live-socket integration subset completed with 9 passed.
Skipped checks required unavailable Node/Deno runtimes or Windows behavior.
One existing subprocess-transport cleanup warning was recorded. Raw JSONL,
test logs, and snapshot manifests are retained outside the checkout. Browser,
frontend, and image evidence is produced by CI for the submitted revision;
consult that run's completed checks and artifacts before accepting delivery.
