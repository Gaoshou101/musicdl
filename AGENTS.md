# Astra-Sol-Luna project rules

The main coordinator owns user intent, permissions, rollback decisions, and the final user-facing result. Astra owns planning, Luna performs concrete work, and Sol independently reviews the settled result. Search output, repetitive test logs, and intermediate exploration should stay in bounded Luna threads whenever practical.

- Use `gpt-6-sol` as the repository default for coordinator and ordinary task work (`medium` by default). Run Astra planning with model `gpt-6-astra` and automatically selected reasoning (`medium` by default). Run Sol review with model `gpt-6-sol` and the same adaptive policy. Use `low` for mechanical work, `high` for demonstrated architecture/security/migration/concurrency/irreversibility risk, and higher levels only when unresolved complexity justifies them.
- Run concrete exploration, implementation, and verification on `gpt-6-luna` with reasoning fixed at `max`. Use generic overridable agents instead of fixed-effort `luna_*` roles when those roles cannot satisfy `max`.
- Each concrete-work wave uses a fixed four-slot topology: the main coordinator directly coordinates exactly three `gpt-6-luna` subagents, each with reasoning fixed at `max`; Astra plans before the wave and Sol independently reviews after it. Assign all three Luna workers useful independent work, prohibit further delegation, run dependent or shared-file/code-area work serially, and perform final verification only after the edits settle. If host capacity cannot provide all three Luna workers, report the shortfall truthfully rather than silently changing the topology.
- Read-intensive, mutually independent tasks may run in parallel. Tasks that modify the same file or code region must run serially.
- Without separate worktrees, never allow multiple subagents to edit the same code area concurrently.
- Every delegation must state its objective, allowed scope, prohibited scope, known context, completion criteria, verification, rollback, and required structured return.
- Luna finishing a task does not mean it passed. Independent Sol review must inspect the real diff, changed-file scope, and completed test evidence before acceptance.
- Never treat “the test command started” as “the tests passed.” Record the completed result and exit status or equivalent evidence.
- Key conclusions require traceable evidence such as a file and symbol, command output, test result, or configuration value.
- Keep changes minimal. Do not opportunistically refactor unrelated code, add production dependencies, or alter architecture outside the approved task.
- A Luna agent must stop and report ambiguity, risk, or required work outside its authorization instead of expanding scope.
- Do not use fixed-effort `sol_escalation` for ordinary review. Use an explicitly configured Sol reviewer and select its reasoning from task risk; reserve High or above for qualified cross-system architecture, migration or irreversible change, security or permission risk, concurrency or consistency risk, major long-term tradeoffs, two failed Medium analyses, or credible production/data-loss risk.
- After Sol accepts a change and its commit succeeds, promptly push the current branch to `origin` (Gaoshou101/musicdl) so GitHub stays synchronized.
- Never force-push automatically, and never push failed or unaccepted work; report authentication, remote, rejection, or CI blockers accurately.
- For every numbered release, synchronize package/version metadata, `README.md`, `README.zh-CN.md`, the GitHub Release, and both version-tagged Docker images; verify all release outputs share the same source commit and provenance, and confirm CI has completed successfully before announcing.
