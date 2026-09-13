# Phase 6 Restricted Plugin Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver versioned Python and JavaScript source plugins that participate in music search while hostile code is contained by a stateless runner, per-job controls, deny-by-default runtimes, and a main-owned HTTPS action broker.

**Architecture:** The main service validates and stores immutable plugin source, then invokes the existing isolated `plugin-runner` by value. Python runs under a libseccomp/rlimit bootstrap and JavaScript under pinned Deno with no permissions; plugins can request at most four HTTPS actions, which the main service validates, DNS-pins, and executes. Runtime acceptance requires hostile Docker tests in addition to unit and static Compose checks.

**Tech Stack:** Python 3.12.14, FastAPI 0.141.1, Pydantic 2, HTTPX 0.28.1 for runner RPC, Python stdlib TLS/socket/http.client for DNS-pinned HTTPS, Deno 2.9.6, Debian Bookworm libseccomp2 2.5.4-1+deb12u1, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-12-phase-6-plugin-runtime-design.md`

## Global Constraints

- Preserve `musicdl.plugin/v1`; plugin request/result JSON remains at most 64 KiB and timeout remains 1-30 seconds.
- Source is UTF-8, NUL-free, and at most 128 KiB; languages are exactly `python` and `javascript`.
- The runner receives no volume, secret, API key, Telegram session, Docker socket, host mount, published port, proxy setting, or external network.
- Plugins have no direct file, environment, network, package-install, subprocess, or process-inspection capability.
- Main-mediated HTTP permits only exact allowlisted HTTPS DNS hosts on port 443, no redirects, credentials, proxies, ambient auth/cookies, or non-global resolved IPs.
- Runner limits are two concurrent jobs, 512 MiB container memory, 1 CPU, 64 PIDs, and 32 MiB hardened tmpfs.
- Per-job limits are wall time 1-30 seconds, CPU 5 seconds, Python AS 256 MiB, Deno heap 128 MiB, file size 1 MiB, 32 FDs, zero core, 64 KiB stdout, and 16 KiB stderr.
- Phase 7 owns authenticated public upload/UI; this phase provides the internal immutable store and source adapter only.
- No production dependency may be added except the approved Deno/libseccomp runtime components; record exact versions, licenses, URLs, and checksums.
- Preserve unrelated user changes and commit only the files named by each task.

---

### Task 1: Extend the versioned plugin contract

**Files:**
- Modify: `src/musicdl/contracts/plugin.py`
- Modify: `src/musicdl/contracts/__init__.py`
- Modify: `tests/unit/test_plugin_contract.py`

**Interfaces:**
- Consumes: existing `PROTOCOL`, `Operation`, `PluginRequest`, `PluginResponse`, and `MAX_PAYLOAD_BYTES`.
- Produces: `PluginManifest`, `PluginLanguage`, `HttpAction`, `HttpObservation`, `PluginInvocation`, `PluginStep`, `MAX_SOURCE_BYTES`, `MAX_ACTION_BODY_BYTES`, `MAX_INVOCATION_BYTES`, and `MAX_HTTP_ACTIONS`.

- [ ] **Step 1: Write failing manifest and invocation tests**

Add tests that construct this valid shape and reject every boundary independently:

```python
manifest = PluginManifest(
    plugin_id="demo.source",
    version="1.0.0",
    language="python",
    operations=["search"],
    allowed_hosts=["api.example.com"],
    sha256="a" * 64,
)
invocation = PluginInvocation(
    manifest=manifest,
    source="def handle(request): return request",
    request=PluginRequest(
        protocol=PROTOCOL,
        request_id="123e4567-e89b-12d3-a456-426614174000",
        operation="search",
        payload={"query": "song"},
    ),
)
assert invocation.manifest.plugin_id == "demo.source"
```

Reject unsafe IDs, duplicate operations/hosts, IP literals, wildcard/scheme/port/path hosts, uppercase hosts, wrong digest, NUL source, source over 128 KiB, digest mismatch, undeclared operation, observations over four, action methods other than GET, response bodies over 1 MiB, and a `PluginStep` containing both/neither response and action.

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_contract.py -q`

Expected: collection/import failure for the new contract classes.

- [ ] **Step 3: Implement strict Pydantic models**

Use these constants and field shapes:

```python
MAX_SOURCE_BYTES = 128 * 1024
MAX_ACTION_BODY_BYTES = 1024 * 1024
MAX_INVOCATION_BYTES = 6 * 1024 * 1024
MAX_HTTP_ACTIONS = 4
PluginLanguage = Literal["python", "javascript"]

class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plugin_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)
    language: PluginLanguage
    operations: tuple[Operation, ...]
    allowed_hosts: tuple[str, ...] = ()
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
```

Normalize printable version text; require unique non-empty operations; validate exact lowercase IDNA DNS hosts with neither IP literals nor wildcard syntax. `PluginInvocation` verifies UTF-8 size, NUL absence, SHA-256 equality, declared operation, observation count, matching action IDs, each decoded body at most 1 MiB, and total serialized invocation at most 6 MiB. `HttpAction` permits GET HTTPS URLs only at the structural layer; the broker performs destination policy. `PluginStep` uses an after-validator to require exactly one of `response` or `action`.

- [ ] **Step 4: Run contract tests and confirm GREEN**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_contract.py -q`

Expected: all plugin contract tests pass.

- [ ] **Step 5: Commit the contract slice**

```powershell
git add src/musicdl/contracts/plugin.py src/musicdl/contracts/__init__.py tests/unit/test_plugin_contract.py
git commit -m "feat(plugin): define restricted runtime contract"
```

---

### Task 2: Add immutable main-side plugin storage

**Files:**
- Create: `src/musicdl/plugins/__init__.py`
- Create: `src/musicdl/plugins/store.py`
- Create: `tests/unit/test_plugin_store.py`

**Interfaces:**
- Consumes: `PluginManifest`, `MAX_SOURCE_BYTES`, and application-data root supplied by the caller.
- Produces: `StoredPlugin(manifest, path, enabled)`, `PluginStore.install(*, plugin_id, version, language, operations, allowed_hosts, source)`, `PluginStore.load(plugin_id, sha256)`, `PluginStore.enabled()`, and `PluginStore.set_enabled(plugin_id, sha256, enabled)`.

- [ ] **Step 1: Write failing store tests**

Use `tmp_path` to prove install computes SHA-256, writes
`plugins/<plugin-id>/<digest>.py|js`, mode `0600`, and returns identical content
on load. Add tests for no overwrite, mismatched existing bytes, symlinked plugin
directories/files, traversal IDs, atomic cleanup after a mocked replace failure,
and independent enable/disable state in `plugins/registry.json`.

```python
store = PluginStore(tmp_path)
stored = store.install(
    plugin_id="demo", version="1", language="python",
    operations=("search",), allowed_hosts=(), source=SOURCE,
)
assert stored.path.read_bytes() == SOURCE.encode("utf-8")
assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600
assert store.load("demo", stored.manifest.sha256).manifest == stored.manifest
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_store.py -q`

Expected: import failure for `musicdl.plugins.store`.

- [ ] **Step 3: Implement exclusive immutable storage**

Resolve the configured root once, create directories with mode `0700`, reject
symlinks using `lstat`, and on POSIX create source with `os.open(..., O_CREAT |
O_EXCL | O_WRONLY | O_NOFOLLOW, 0o600)`. Write and `fsync`; if the digest path exists,
verify its bytes and metadata instead of replacing it. Persist the enabled
registry by writing a same-directory temporary JSON file, `fsync`, and
`os.replace`. Never accept a caller-provided path or digest.

Keep the store functional on Windows for unit development, but mark POSIX mode,
`O_NOFOLLOW`, and symlink-race assertions as Linux-only; Docker acceptance is
the authority for those security properties.

- [ ] **Step 4: Run store and contract tests**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_store.py tests/unit/test_plugin_contract.py -q`

Expected: both files pass.

- [ ] **Step 5: Commit the storage slice**

```powershell
git add src/musicdl/plugins/__init__.py src/musicdl/plugins/store.py tests/unit/test_plugin_store.py
git commit -m "feat(plugin): store immutable plugin versions"
```

---

### Task 3: Implement the DNS-pinned HTTPS action broker

**Files:**
- Create: `src/musicdl/plugins/broker.py`
- Create: `tests/unit/test_plugin_broker.py`

**Interfaces:**
- Consumes: `HttpAction`, exact `allowed_hosts`, injected resolver/socket/TLS factories for deterministic tests.
- Produces: `HttpsActionBroker.fetch(action, allowed_hosts) -> HttpObservation` and `ActionDenied(code)`.

- [ ] **Step 1: Write failing SSRF and transport tests**

Cover valid exact-host HTTPS, IDNA normalization, URL credentials, explicit
ports, fragments, redirects, oversized `Content-Length`, streamed body over 1
MiB, malformed HTTP, proxy environment variables, and all non-global IPv4/IPv6
classes. A resolver returning both a global and private address must be rejected.
Prove the connector receives the validated numeric IP while TLS receives the
original approved hostname and the `Host` header contains that hostname.

```python
broker = HttpsActionBroker(
    resolver=lambda host, port: [(socket.AF_INET, ("93.184.216.34", 443))],
    connector=fake_connect,
    ssl_context=fake_tls,
)
observation = broker.fetch(action("https://api.example.com/search?q=x"), ("api.example.com",))
assert fake_connect.address == ("93.184.216.34", 443)
assert fake_tls.server_hostname == "api.example.com"
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_broker.py -q`

Expected: import failure for `musicdl.plugins.broker`.

- [ ] **Step 3: Implement URL policy and pinned HTTP/1.1 transport**

Parse with `urlsplit`; require scheme `https`, hostname, implicit port 443, no
userinfo/fragment, and exact normalized allowlist match. Resolve with
`socket.getaddrinfo(..., type=SOCK_STREAM)`, convert each address through
`ipaddress.ip_address`, and require every candidate to have `is_global is True`.
Sort/deduplicate numeric candidates, connect directly to one numeric address,
then call `SSLContext.wrap_socket(sock, server_hostname=approved_host)` so
certificate validation and SNI remain hostname-based without a second DNS lookup.
Send only `Host`, `Accept: application/json`, and `Connection: close`; parse with
`http.client.HTTPResponse`, reject 3xx, and read at most 1 MiB plus one byte.
Clear proxy influence by never consulting environment proxy settings.

- [ ] **Step 4: Run broker and contract tests**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_broker.py tests/unit/test_plugin_contract.py -q`

Expected: both files pass.

- [ ] **Step 5: Commit the broker slice**

```powershell
git add src/musicdl/plugins/broker.py tests/unit/test_plugin_broker.py
git commit -m "feat(plugin): broker allowlisted HTTPS actions"
```

---

### Task 4: Build the runner supervisor and execution endpoint

**Files:**
- Create: `src/musicdl_plugin_runner/supervisor.py`
- Modify: `src/musicdl_plugin_runner/app.py`
- Create: `tests/unit/test_plugin_supervisor.py`
- Create: `tests/unit/test_plugin_runner_app.py`

**Interfaces:**
- Consumes: `PluginInvocation`; executable builders for Python and Deno hosts.
- Produces: `Supervisor.execute(invocation) -> PluginStep` and `POST /v1/execute`.

- [ ] **Step 1: Write failing supervisor lifecycle tests**

Use helper child commands to test valid JSON, invalid/oversized stdout, bounded
stderr, non-zero exit, wall timeout, concurrency rejection at three simultaneous
jobs, a child that traps TERM, and a child that creates a descendant. Assert the
whole process group is gone after TERM, 250 ms grace, and KILL. Test endpoint
status mapping without exposing source, stderr, request payload, or exception text.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_supervisor.py tests/unit/test_plugin_runner_app.py -q`

Expected: imports/routes are missing.

- [ ] **Step 3: Implement bounded asynchronous supervision**

Use an `asyncio.Semaphore(2)` and `asyncio.create_subprocess_exec` with an empty
environment, `stdin/stdout/stderr=PIPE`, `close_fds=True`, and a new POSIX session.
Serialize invocation JSON once and reject it before process creation if bounded
contract serialization exceeds its limit. Read stdout/stderr through bounded
stream readers that terminate the group on overflow. Use `wait_for` for wall
time; on timeout/overflow call `os.killpg(pid, SIGTERM)`, wait 250 ms, then
`SIGKILL`. Convert all failures to stable `PluginStep` error responses.

Add `POST /v1/execute` to FastAPI. Read `Request.stream()` manually and reject
the body once it exceeds `MAX_INVOCATION_BYTES` before Pydantic validation, so
chunked requests cannot bypass a `Content-Length` check. Use one process-wide
supervisor, return 429 when both slots are occupied, and retain `/healthz`.

- [ ] **Step 4: Run supervisor, endpoint, and existing tests**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_supervisor.py tests/unit/test_plugin_runner_app.py tests/unit/test_plugin_contract.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the supervisor slice**

```powershell
git add src/musicdl_plugin_runner/supervisor.py src/musicdl_plugin_runner/app.py tests/unit/test_plugin_supervisor.py tests/unit/test_plugin_runner_app.py
git commit -m "feat(plugin): supervise bounded plugin jobs"
```

---

### Task 5: Add restricted Python and Deno hosts

**Files:**
- Create: `src/musicdl_plugin_runner/python_host.py`
- Create: `src/musicdl_plugin_runner/deno_host.js`
- Create: `src/musicdl_plugin_runner/seccomp.py`
- Create: `tests/unit/test_python_plugin_host.py`
- Create: `tests/unit/test_deno_plugin_host.py`

**Interfaces:**
- Consumes: serialized `PluginInvocation` on stdin and host command selection from `Supervisor`.
- Produces: one serialized `PluginStep` on stdout; no other observable output or retained state.

- [ ] **Step 1: Write failing valid and hostile host tests**

Valid Python and JavaScript fixtures define `handle(request)` and return the
same deterministic search response. Hostile Python attempts `open`, `__import__`,
`/proc/1/environ`, socket creation, `os.fork`, `subprocess`, `ctypes`, signal,
and infinite CPU. Hostile JavaScript attempts `process`, `require`, `fetch`,
`Deno.env`, `Deno.readTextFile`, `Deno.Command`, FFI, Worker, and infinite CPU.
All denial results must use stable codes and omit attacker-controlled exception
text. Mark Linux-only seccomp tests explicitly and skip only when the kernel or
Deno executable is unavailable.

- [ ] **Step 2: Run host tests and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_python_plugin_host.py tests/unit/test_deno_plugin_host.py -q`

Expected: host modules are missing.

- [ ] **Step 3: Implement Python resource and syscall lockdown**

Before executing source, set `RLIMIT_CPU=(5,5)`, `RLIMIT_AS=256*1024*1024`,
`RLIMIT_FSIZE=1024*1024`, `RLIMIT_NOFILE=(32,32)`, and `RLIMIT_CORE=(0,0)`.
Compile source in memory, clear `os.environ`, change into the supervisor-created
job directory, then load libseccomp through `ctypes.CDLL("libseccomp.so.2")`.
Create an allow-default filter whose deny rules return `EPERM` for open/openat/
openat2/creat, socket/socketpair/connect/bind/listen/accept/accept4, clone/
clone3/fork/vfork, execve/execveat, ptrace/process_vm_readv/process_vm_writev,
kill/tkill/tgkill/pidfd_open/pidfd_getfd/pidfd_send_signal, setsid/setpgid,
mount/umount2/pivot_root/chroot/unshare/setns, keyctl/add_key/request_key, bpf,
perf_event_open, and userfaultfd. Missing syscall names are ignored only when
libseccomp reports they do not exist on the current architecture.

Execute with a fixed builtins dictionary containing only pure value/control
helpers (`abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `float`, `int`, `len`,
`list`, `max`, `min`, `range`, `reversed`, `round`, `sorted`, `str`, `sum`,
`tuple`, `zip`, `Exception`, `ValueError`). Require exactly one callable
`handle`; JSON-serialize its return under the existing response limit.

- [ ] **Step 4: Implement the Deno no-permission bootstrap**

`deno_host.js` reads the invocation embedded by the trusted launcher, removes
`Worker`, freezes the request, evaluates source in a wrapper with no Node
compatibility imports, requires one `handle`, and emits one JSON result. Invoke
Deno as:

```text
deno run --quiet --no-config --no-lock --no-npm --cached-only --v8-flags=--max-old-space-size=128 -
```

Do not pass any `--allow-*` permission. Set `DENO_NO_PROMPT=1`, clear all other
environment variables, and make the test assert the exact argv and environment.

- [ ] **Step 5: Run host and supervisor tests**

Run: `.venv\Scripts\python -m pytest tests/unit/test_python_plugin_host.py tests/unit/test_deno_plugin_host.py tests/unit/test_plugin_supervisor.py -q`

Expected: all tests available on the current platform pass; skips list only
Linux/Deno-unavailable runtime cases.

- [ ] **Step 6: Commit the host slice**

```powershell
git add src/musicdl_plugin_runner/python_host.py src/musicdl_plugin_runner/deno_host.js src/musicdl_plugin_runner/seccomp.py tests/unit/test_python_plugin_host.py tests/unit/test_deno_plugin_host.py
git commit -m "feat(plugin): sandbox Python and JavaScript hosts"
```

---

### Task 6: Connect stored plugins to the source registry

**Files:**
- Create: `src/musicdl/plugins/client.py`
- Create: `src/musicdl/plugins/source.py`
- Modify: `src/musicdl/plugins/__init__.py`
- Create: `tests/unit/test_plugin_client.py`
- Create: `tests/unit/test_plugin_source.py`

**Interfaces:**
- Consumes: `PluginStore`, `HttpsActionBroker`, runner service URL, `PluginInvocation`, `PluginStep`, `Candidate`, and `MusicSource`.
- Produces: `PluginClient.invoke(stored, request) -> PluginResponse` and `PluginSource.search(query) -> tuple[Candidate, ...]`.

- [ ] **Step 1: Write failing action-loop and source-adapter tests**

Test direct final responses, one through four broker actions, a fifth action,
repeated/mismatched action IDs, runner timeout/HTTP/invalid JSON, denied broker
actions, digest mismatch, operation mismatch, candidate source/version mismatch,
oversized candidate lists, disabled plugins, and successful registration in
`SourceRegistry`.

```python
source = PluginSource(stored, client)
candidates = asyncio.run(source.search("song"))
assert candidates == (Candidate(
    source_id=stored.manifest.plugin_id,
    source_version=stored.manifest.version,
    item_id="1", title="Song", artist="Artist",
),)
```

- [ ] **Step 2: Run client/source tests and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_client.py tests/unit/test_plugin_source.py -q`

Expected: client and adapter modules are missing.

- [ ] **Step 3: Implement bounded runner RPC and capability loop**

Use an injected `httpx.AsyncClient` with no inherited proxy environment
(`trust_env=False`), no redirects, and timeout no greater than the remaining
top-level request budget. Send only the invocation JSON to `/v1/execute`.
Validate every body as `PluginStep`. For an action, call the broker through
`asyncio.to_thread`, append the matching observation, and reinvoke; reject a
fifth step or repeated ID. Return stable errors without response-body text.

- [ ] **Step 4: Implement the search adapter**

Create a search `PluginRequest` with a UUID and normalized query, invoke the
client, require a successful list result of at most 100 candidates, validate each
as `Candidate`, and enforce manifest ID/version equality. The adapter exposes no
download side effect. Enablement remains controlled by `PluginStore` and
`SourceRegistry`.

- [ ] **Step 5: Run plugin and source regression tests**

Run: `.venv\Scripts\python -m pytest tests/unit/test_plugin_client.py tests/unit/test_plugin_source.py tests/unit/test_source_registry.py tests/unit/test_source_search.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the integration slice**

```powershell
git add src/musicdl/plugins/client.py src/musicdl/plugins/source.py src/musicdl/plugins/__init__.py tests/unit/test_plugin_client.py tests/unit/test_plugin_source.py
git commit -m "feat(plugin): integrate restricted plugin sources"
```

---

### Task 7: Harden the plugin image and Compose contract

**Files:**
- Modify: `docker/plugin/Dockerfile`
- Modify: `compose.yaml`
- Modify: `tests/unit/test_dockerfiles.py`
- Modify: `tests/unit/test_compose_contract.py`
- Modify: `THIRD_PARTY.md`

**Interfaces:**
- Consumes: runner executable requirements from Tasks 4-5.
- Produces: reproducible amd64/arm64 runner image with Deno 2.9.6 and libseccomp2 2.5.4-1+deb12u1 plus enforced Compose limits.

- [ ] **Step 1: Write failing static supply-chain and Compose tests**

Require `python:3.12.14-slim-bookworm`, `DENO_VERSION=2.9.6`, both official
release checksums, checksum verification before unzip, pinned
`libseccomp2=2.5.4-1+deb12u1`, and `deno --version` during build. Require runner
`mem_limit: 512m`, `cpus: "1.0"`, `pids_limit: 64`, `init: true`, and exact tmpfs
`/tmp:size=32m,noexec,nosuid,nodev`. Continue asserting no volumes, secrets,
Docker socket, Redis/AI/Telegram environment, port, or external network.

- [ ] **Step 2: Run static tests and confirm RED**

Run: `.venv\Scripts\python -m pytest tests/unit/test_dockerfiles.py tests/unit/test_compose_contract.py -q`

Expected: missing Deno/seccomp and resource-limit assertions fail.

- [ ] **Step 3: Build a checksum-verified multi-architecture Dockerfile**

Use a download stage with `TARGETARCH`. Download only from
`https://github.com/denoland/deno/releases/download/v2.9.6/` and verify before
unzip:

```text
amd64  deno-x86_64-unknown-linux-gnu.zip
       394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2
arm64  deno-aarch64-unknown-linux-gnu.zip
       9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e
```

The final `python:3.12.14-slim-bookworm` stage installs only
`ca-certificates` and `libseccomp2=2.5.4-1+deb12u1` with no recommends, copies
the verified Deno binary, removes apt lists, and retains UID/GID 10001.

- [ ] **Step 4: Apply Compose limits and document dependencies**

Add the exact runner limits from Step 1. Record Deno MIT license, release URL,
both checksums, and libseccomp LGPL-2.1 license/version/source in
`THIRD_PARTY.md`; explain that neither component is exposed to plugins as an
install surface.

- [ ] **Step 5: Run static tests**

Run: `.venv\Scripts\python -m pytest tests/unit/test_dockerfiles.py tests/unit/test_compose_contract.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the deployment slice**

```powershell
git add docker/plugin/Dockerfile compose.yaml tests/unit/test_dockerfiles.py tests/unit/test_compose_contract.py THIRD_PARTY.md
git commit -m "build(plugin): harden the restricted runner image"
```

---

### Task 8: Add hostile Docker acceptance tests and close Phase 6

**Files:**
- Create: `tests/integration/test_plugin_runtime.py`
- Create: `tests/fixtures/plugins/minimal.py`
- Create: `tests/fixtures/plugins/minimal.js`
- Create: `tests/fixtures/plugins/hostile.py`
- Create: `tests/fixtures/plugins/hostile.js`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Create: `.planning/phases/06-restricted-plugin-runtime/SUMMARY.md`

**Interfaces:**
- Consumes: built Compose services, `/v1/execute`, valid/hostile fixtures, and Docker CLI.
- Produces: completed static/unit/runtime evidence for the mandatory Phase 6 release gate.

- [ ] **Step 1: Write the Docker-only acceptance suite**

Register the `integration` marker in `pyproject.toml`. Mark tests `integration`
and skip with the exact reason `Docker Engine is not
available` only when `docker version` fails. Build and start Compose with a test
external Redis URL that does not expose credentials to the runner. Execute valid
Python/JS search plugins, then hostile attempts covering environment canaries,
`/proc/1/environ`, `/etc/shadow`, `/run/docker.sock`, Telegram-session paths,
open/socket/connect to main and public IPs, subprocess/fork/commands, infinite
CPU, memory, PID/tmp/output exhaustion, traversal, and retained files. After
every denial assert `/healthz` succeeds and canary values do not appear in body
or `docker compose logs plugin-runner`.

- [ ] **Step 2: Run the new suite and confirm it detects an unbuilt runtime**

Run: `.venv\Scripts\python -m pytest tests/integration/test_plugin_runtime.py -q -rs`

Expected before build/start: explicit Docker-unavailable skip or connection/build
failure; never a false pass.

- [ ] **Step 3: Build and run the actual services**

Run:

```powershell
docker compose build --no-cache plugin-runner
docker compose config
docker compose up -d plugin-runner
.venv\Scripts\python -m pytest tests/integration/test_plugin_runtime.py -q -rs
docker compose ps
```

Expected: build exit 0; config exit 0; hostile suite passes with zero skips;
runner is healthy after the final attack. If the Windows host has no Docker
client, run the same commands on the target Debian-like host; do not mark Phase 6
accepted from static tests alone.

- [ ] **Step 4: Run the complete regression suite**

Run: `.venv\Scripts\python -m pytest -q -rs`

Expected: exit 0; no unexpected skip, warning, or failure. Record exact passed/
skipped counts and duration.

- [ ] **Step 5: Update documentation and phase evidence**

Replace README statements that execution/egress are disabled with the restricted
capability model, manifest example, supported entry points, resource limits,
and explicit incompatibilities. In `SUMMARY.md`, map FR-003/FR-010/NFR-001/
NFR-002 to files, completed commands, exit statuses, Docker/OS versions, attack
results, residual limitations, and rollback. Leave the release gate open if the
Docker suite could not run.

- [ ] **Step 6: Commit the acceptance slice**

```powershell
git add tests/integration/test_plugin_runtime.py tests/fixtures/plugins pyproject.toml README.md .planning/phases/06-restricted-plugin-runtime/SUMMARY.md
git commit -m "test(plugin): verify hostile runtime isolation"
```

- [ ] **Step 7: Sol final acceptance and synchronization**

Sol inspects `git diff origin/codex/phase-6-plugin-runtime...HEAD`, confirms each
commit contains only authorized files, reruns the completed unit suite, verifies
Docker evidence rather than launched commands, and checks `git status --short`.
After acceptance, push normally:

```powershell
git push origin codex/phase-6-plugin-runtime
```

Never force-push. If authentication, rejection, Docker availability, or hostile
runtime checks fail, report the blocker accurately and do not claim Phase 6 or
the 1.0 plugin security gate is complete.
