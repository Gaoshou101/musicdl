# Administration and performance optimization

Source baseline: musicdl 1.1.0, `a9b4fa1d2b384c05178a09cce7b0c4983dd07672`.

This work implements the next-step administration and performance plan. It does
not change the numbered release or deploy production services.

## Scope

- Remembered browser sessions, authenticated session recovery, and logout.
- Recent real search/download requests shown as timestamped status bars, while
  preserving the independent search, download, and lossless-quality metrics.
- Bounded source diagnostics with clear search/resolve outcomes.
- Shared download admission and cancellation-safe local media work.
- Small dependency cleanup and a reproducible local media benchmark.

The main wheel continues to contain the plugin runner: installed-package tests
explicitly require that distribution contract. Existing public brand resource
paths remain supported. Removing an unused build dependency does not itself
establish a browser bundle or container size reduction.

Dependency cleanup removes the unused `motion` declaration and four orphaned
lockfile entries (`motion`, `framer-motion`, `motion-dom`, `motion-utils`). Other
resolved dependency versions and shared `tslib` consumers are retained. Full
static-export and container builds are required in CI; no compressed bundle or
container size reduction is claimed from this dependency change alone.

## Operational behavior

Remembered logins have a 30-day lifetime; ordinary browser session cookies have
a 12-hour server lifetime. The existing atomic administrator state persists
both session types as token/CSRF hashes with expiry and credential version.
Logout revokes a session and credential changes invalidate all old sessions.
No password or bearer token is stored in browser application storage.

Source diagnostics have two workers, up to 32 waiting jobs, a 45-second overall
deadline including the queue, and a 60-second cooldown. Their bounded atomic
result file is separate from credential state. Search and quality-aware link
resolution are probed without downloading media or recording real traffic
metrics. A resolved link remains labeled “Download not yet verified.”

One application-owned download budget is shared across panel requests and
background workers and retained across runtime reloads: four active downloads,
sixteen pending, and at most two active per source/user. This is per process;
multiple processes do not receive deployment-wide coordination from it.
Matching panel download requests share one underlying operation and history row.
The full operation deadline includes admission waiting and preserves declared
slow-source stream budgets. Runtime retirement and the last waiter cancelling
drain the underlying operation before releasing borrowed resources.
Local media work uses two bounded executor threads. Cancellation waits for
actual work completion before cleanup and preserves the existing publication
fencing and terminal-state semantics.

## Benchmark method

`scripts/performance/benchmark_media_download.py` uses a fixed, synthetic 4 MiB
MP3-shaped stream and the actual media download path. It records task latency,
throughput, event-loop lag, peak RSS, and thread count. No public music source is
queried, and its bytes are not a real audio fixture.

```sh
PYTHONPATH=src python scripts/performance/benchmark_media_download.py \
  --size-mib 4 --rounds 20 --concurrency 1,4,8,16 --seed 1296671049
```

The same script can measure the baseline by selecting the baseline source tree
with `PYTHONPATH`. Use the same Python, workload, filesystem, and resource limits.
The peak scratch payload budget is 64 MiB; completed round files are removed.
These results describe local processing and responsiveness, not production
network throughput or a forecast of upstream download speed.

| Concurrency | Loop lag p95, baseline → updated | Download p95, baseline → updated | Throughput, baseline → updated |
| --- | --- | --- | --- |
| 1 | 9.76 → 4.32 ms | 19.38 → 19.45 ms | 227.55 → 224.69 MiB/s |
| 4 | 49.70 → 7.00 ms | 71.41 → 68.31 ms | 241.59 → 241.64 MiB/s |
| 8 | 141.76 → 9.50 ms | 206.37 → 212.04 ms | 197.53 → 193.09 MiB/s |
| 16 | 182.53 → 10.29 ms | 267.91 → 255.27 ms | 244.21 → 243.25 MiB/s |

Both runs completed with exit code 0 on Python 3.12.15, Linux x86_64, with 20
rounds at each concurrency level and a 4,194,613-byte stream. The benchmark script
SHA256 was `55ff91765acd7443edbeafcada5eda8c1c0a88bacb68d5f2d0fba6f546dda158`.
The revised media snapshot SHA256 was
`a83230e41afe9f60a70913546fe3ff93e444a0f6e1eadd481d3cb810701bac2a`;
its admission queue condition was subsequently corrected without changing the
benchmarked media implementation. File hashes were checked separately.

At 16 concurrent downloads, loop lag p95 fell by 94.4%, throughput fell 0.4%,
and download latency p95 fell 4.7%. At eight concurrent downloads, throughput
fell 2.2% and latency p95 rose 2.7%. Peak RSS rose from 42,438,656 to
50,532,352 bytes (7.72 MiB), and peak threads from one to three. This bounded
thread tradeoff improves event-loop responsiveness; it does not demonstrate a
general throughput improvement. These are single matched runs on the same
shared test host, rather than a statistically controlled production study.
The workload exercises local media I/O directly; admission and queue behavior
are verified separately by regression tests.

## Verification baseline

The unchanged source completed 2,029 unit tests with 85 skips, exit code 0.
Skipped capabilities were reported explicitly, including unavailable Deno,
unconfigured live Redis, and platform-specific tests. Baseline frontend service
log, channel metric, search probe, and source import contracts also completed.

The new recent-request slice completed 70 focused backend tests, its frontend
rendering contract, and full TypeScript checking. Its actual component and
panel styles were also inspected with success, failure, excluded, and empty
fixtures. Fixtures were explicitly labeled as demonstration data.

Final remote verification completed 2,082 unit tests with 82 skips, no failures,
and exit code 0 across three disjoint file partitions (690/585/807 passed).
Dedicated real-Redis WeCom and live-socket AI integration completed nine tests,
exit code 0. The final frontend TypeScript check, service-log, channel-metric,
search-probe, source-import, and diagnostic-polling contracts all completed with
exit code 0. The diagnostic-polling harness executes the transpiled page, hook,
and API to check bounded batch polling, non-overlap, navigation cancellation,
terminal notifications, idle stopping, action refresh, and signal forwarding.
An offline `npm ci --dry-run --ignore-scripts` validated the updated dependency
lock without installing it; actual installation and image builds remain CI gates.

The remote source was compared with 310 local file hashes, with no mismatches.
The final implementation snapshot SHA256 was
`060714bd7b1b7a76414c89937ba533d285daed233003e27cdca3e586d066da24`;
one diagnostic test fixture subsequently switched from a private admission
method to the public context manager, and its complete partition and the
remaining partition and integrations passed. Documentation was synchronized
after those completed results; source hashes identify the final tested files.
One earlier cancellation test needed an explicitly blocked operation to avoid
a completion race. The existing lite-mode rejected-configuration test now
captures its byte-for-byte persistence baseline after successful login, because
login legitimately persists session state. Its rejected-write, status, and
configuration assertions remain intact.

Remote Python verification skipped unavailable Node/Deno runtimes and Windows
path behavior. The three fewer skips than baseline are Redis cases enabled by
the isolated test instance. Docker hostile-runtime coverage and full source-image
builds are deferred to the existing PR CI on a hosted runner, since the dedicated
test host has insufficient spare disk for those builds.

Independent review requested three corrections before acceptance: re-acquire a
source permit when fallback changes source, keep cancelled queued password work
inside its capacity budget until actual completion, and replace detached
per-job frontend watchers with one component-owned snapshot poll. Those fixes
completed fresh focused checks (212 media/worker tests, 44 authentication tests,
and all frontend contracts) before the final full run. Three additional real
panel/worker fallback regressions passed, covering convergent source limits,
opposing source transitions with cancellation, and returning to an original
quality candidate only after re-acquiring its source slot.

## Delivery boundaries

Integration work uses isolated data and a dedicated test Redis. Test credentials
and connection details are kept outside the repository. Builds and checks that
cannot run in the constrained test environment remain explicit until CI supplies
the corresponding evidence. Session expiry, logout, password-change revocation,
download cancellation, artifact publication, and existing security assertions
must pass before acceptance.
