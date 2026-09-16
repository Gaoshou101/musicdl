# Phase 6 Restricted Plugin Runtime Design

## Status and scope

This design implements Phase 6 from `.planning/ROADMAP.md` and covers FR-003,
FR-010, NFR-001, and NFR-002. It adds versioned local Python and JavaScript
source plugins while retaining the existing two-application-container
deployment. Phase 7 authentication and administration UI are out of scope, so
Phase 6 exposes an internal storage service but no unauthenticated public upload
endpoint.

The supported plugin model is intentionally restricted. Plugins are pure
request/response programs with optional declarative HTTP actions. They do not
receive ambient filesystem, environment, network, process, package-install, or
host capabilities. Full CPython/Node compatibility and direct `requests` or
`fetch` access are not promised.

## Security boundary

The `plugin-runner` container is the final trust boundary. It remains non-root,
read-only, capability-free, protected by `no-new-privileges`, attached only to
the internal `plugin-control` network, and has no volumes, secrets, Docker
socket, host mounts, or published ports. Every invocation additionally runs in
a disposable subprocess with bounded inputs, outputs, resources, and lifetime.

The main service owns plugin source storage and all privileged actions. It sends
source code and a `musicdl.plugin/v1` request to the runner by value. The runner
does not persist plugins. A plugin may return a bounded declarative HTTP action;
the main service validates and performs that action, then invokes the plugin
again with the bounded observation. Raw plugin code never opens a network
connection.

This design can defend the claims that plugin code cannot directly read main
service secrets, Telegram sessions, API keys, host-sensitive files, or directly
open network connections. It does not claim VM-grade isolation or safe execution
of unrestricted third-party libraries.

## Plugin identity, manifest, and storage

A manifest contains:

- `plugin_id`: 1-64 ASCII characters matching `[A-Za-z0-9][A-Za-z0-9_.-]*`.
- `version`: 1-64 normalized printable characters.
- `language`: exactly `python` or `javascript`.
- `operations`: a non-empty subset of `capabilities`, `search`, `resolve`,
  `download`, and `health`. `download` is a compatibility label only: the main
  service never invokes it and expects `resolve` to return a media descriptor.
- `allowed_hosts`: exact lowercase DNS hostnames; no wildcard, IP literal,
  scheme, port, credentials, or path.
- `sha256`: lowercase digest of the UTF-8 source.

Source is at most 128 KiB and valid UTF-8 without NUL bytes. `PluginStore`
computes the digest rather than trusting the caller and stores immutable source
under `/data/app/plugins/<plugin-id>/<sha256>.<py|js>`. Paths are generated only
from validated values. Creation is atomic and exclusive, symlinks are rejected,
existing content is verified rather than overwritten, and files use mode
`0600`. Enablement is a main-side registry property, so versions can be disabled
without deleting audit evidence.

## Invocation contract and data flow

The existing `PluginRequest` and `PluginResponse` envelope remains authoritative.
An internal runner request adds the validated manifest, source, and digest. The
runner verifies the digest, language, declared operation, source size, and JSON
limits before creating a process.

Each plugin reads one JSON document from standard input and writes one JSON
document to standard output. It exposes a single `handle(request)` entry point.
The result is either:

- a final `PluginResponse`; or
- an HTTP action containing an action ID, `GET` method, approved HTTPS URL, and
  bounded response preference.

At most four HTTP actions are permitted per top-level operation. The main
service executes an accepted action, replaces it with an observation containing
status, a bounded header subset, and at most 1 MiB of response bytes, then
replays the plugin. Media bodies do not travel through plugin stdout; downloads
remain main-owned and pass through the Phase 4 size, type, hash, and destination
validation path.

For a confirmed candidate the main service calls the typed `PluginClient.resolve()`
operation, which returns one `ResolvedMedia` descriptor: the selected
`candidate_id`, an implicit-port `https` `url`, an `extension`, a `media_type`,
and an optional `declared_size`. The descriptor is rejected unless it is bound to
the confirmed candidate's `item_id`, free of URL credentials, ports, and
fragments, and consistent with the media type implied by its extension. The main
process then streams the body itself through `SecureMediaTransport`, which
normalizes the host through IDNA, matches it exactly against `allowed_hosts`,
resolves all A/AAAA answers, rejects non-global addresses, connects to the pinned
numeric address while preserving the approved hostname for TLS SNI, certificate
verification, and the `Host` header, sends no ambient credentials, follows no
redirects, ignores proxy settings, and closes the response. The streaming result
keeps the Phase 4 bounds at 64 KiB per plugin response or result, 1 MiB per
brokered observation, and 500 MiB per published file.

The worker records the effect before it is acknowledged: media is validated and
atomically published by the main process, and the Redis stream entry is
acknowledged (XACK) only after the recorded effect for that stage has completed.
Each side effect is fenced by a durable job-effect marker, so a replanned job
does not repeat a download, refresh, health probe, rebind, or notice that
already completed.

Search results are validated as existing `Candidate` objects and must match the
manifest plugin ID and version. A `PluginSource` adapter implements the Phase 2
`MusicSource` protocol so enabled direct plugins participate in deterministic
search and remain independently enableable. `PluginSource.download()` is the
plugin-backed `DownloadSource`: it calls the typed `PluginClient.resolve()` for
the selected candidate and streams the returned descriptor through the
main-owned `SecureMediaTransport`, and it never calls the manifest's
compatibility `download` operation.

Contract failures use stable, non-sensitive codes such as `invalid_plugin`,
`invalid_output`, `operation_not_allowed`, `timeout`, `resource_limit`, and
`action_denied`. Raw source, stderr, exception text, HTTP bodies, credentials,
and complete sensitive responses are never logged.

## Python execution

Python starts in isolated mode with a cleared environment, closed inherited
descriptors, and a fresh `/tmp/job-*` working directory. A trusted bootstrap
loads the request and source into memory, installs resource limits, then installs
a libseccomp deny policy before calling plugin code.

The policy denies file open/create calls after bootstrap, network and socket
calls, fork/clone/vfork, further program execution, ptrace/process-memory access,
cross-process signalling, namespace/mount operations, keyrings, BPF, and
performance events. Only explicitly preloaded pure-data helpers are provided.
Removing import builtins and validating the entry point are defense in depth;
they are not described as the security boundary.

## JavaScript execution

JavaScript uses a pinned Deno runtime with no permissions, no prompt, no config,
no lock update, no npm or remote imports, no inherited environment, and a 128 MiB
V8 heap. Source and request are supplied without a writable source file.
`require` and `process` are unavailable. `fetch`, filesystem and environment
access, FFI, workers, and `Deno.Command` must fail. Output is accepted only after
bounded JSON and protocol validation.

Node `vm`, AST filtering, or removed globals are not treated as isolation
mechanisms and are not used as substitutes for Deno permissions.

## Resource and lifecycle controls

The runner admits at most two concurrent jobs. Per invocation:

- wall-clock timeout: the request value, constrained to 1-30 seconds;
- CPU time: 5 seconds;
- Python address space: 256 MiB;
- JavaScript V8 heap: 128 MiB;
- created file size: 1 MiB; open files: 32; core size: zero;
- stdout/result: 64 KiB; stderr capture: 16 KiB;
- all inherited descriptors except stdin/stdout/stderr are closed.

The supervisor creates a process group. On timeout it terminates the group,
waits 250 ms, then kills the entire group. Error output is truncated and reduced
to stable audit fields. Temporary job directories are removed after every
outcome.

Compose additionally sets the runner to 512 MiB memory, 1.0 CPU, 64 PIDs, and a
32 MiB `/tmp` tmpfs with `noexec,nosuid,nodev`. A job failure must not make the
health endpoint unavailable for the next request.

## Network action broker

Only `GET https://<exact-host>/...` on port 443 is supported in Phase 6. Requests
with URL credentials, explicit ports, wildcard hosts, proxy settings, redirects,
ambient cookies, or ambient authorization headers are rejected.

For each action the broker:

1. Matches the normalized hostname exactly against the manifest allowlist.
2. Resolves all A and AAAA candidates.
3. Rejects loopback, private, link-local, multicast, unspecified, reserved, and
   other non-global addresses.
4. Pins an accepted address for the actual connection while preserving the
   approved hostname for TLS SNI and certificate verification.
5. Does not follow redirects and caps the response body at 1 MiB.

The connecting client must not resolve the hostname again after validation.
These rules, combined with denial of raw plugin sockets, prevent bypass through
DNS rebinding, redirects, encoded private addresses, or alternate ports.

## Verification and acceptance

Unit tests cover models, immutable storage, digest checks, source/result bounds,
operation authorization, candidate adaptation, timeout/error mapping, broker URL
and IP policy, redirect rejection, DNS pinning behavior, and static Compose and
Dockerfile contracts.

Linux/Docker runtime tests execute both valid and hostile Python/JavaScript
fixtures. They attempt environment and secret reads, `/proc` and host-file reads,
path traversal, raw sockets, main-service access, subprocess/fork creation,
command execution, infinite loops, memory/PID/tmp/output exhaustion, and
cross-invocation persistence. Tests also prove that known canary secrets and
Telegram session contents never appear in responses or logs, and that the runner
remains healthy after denial or termination.

Static YAML tests do not prove kernel, cgroup, seccomp, Deno, process-group, DNS,
or filesystem isolation. Phase 6 is accepted only after the hostile runtime suite
finishes successfully against the built Compose services on Docker Engine in a
Debian-like Linux environment. Any skipped runtime security test leaves the MVP
plugin safety gate open.

## Dependencies and supply-chain record

The plugin image pins the Deno release artifact and checksum and installs the
Debian libseccomp runtime needed by the Python bootstrap. Exact versions,
licenses, upstream URLs, checksums, and their isolation role are recorded in
`THIRD_PARTY.md`. No plugin-requested package installation is supported.

## Rollback

Disable the affected immutable plugin version and stop invoking the runner, then
redeploy the prior runner image and Compose revision. The runner owns no durable
state and needs no migration rollback. Main-side immutable plugin records may
remain disabled for audit purposes.

## Explicit deferrals

- Authenticated public upload and management UI belong to Phase 7.
- Full Node/LX compatibility, arbitrary Python packages, and direct plugin
  networking are unsupported because they contradict the approved security
  guarantees under the fixed two-container/no-Docker-socket architecture.
- Requiring any of those capabilities reopens the architecture decision and
  prevents Phase 6 acceptance until a per-job container or VM sandbox is
  authorized.
