# Sol-Luna project rules

The main Sol thread owns requirements, architectural decisions, risk decisions, acceptance, rollback decisions, and the final user-facing result. Search output, repetitive test logs, and intermediate exploration should stay in bounded Luna threads whenever practical.

- Use `luna_explorer` for read-only searches and call-chain evidence, `luna_implementer` for small reversible edits, and `luna_tester` for independent test or log evidence.
- Read-intensive, mutually independent tasks may run in parallel. Tasks that modify the same file or code region must run serially.
- Without separate worktrees, never allow multiple subagents to edit the same code area concurrently.
- Every delegation must state its objective, allowed scope, prohibited scope, known context, completion criteria, verification, rollback, and required structured return.
- Luna finishing a task does not mean it passed. Sol must review the real diff, changed-file scope, and test evidence before acceptance.
- Never treat “the test command started” as “the tests passed.” Record the completed result and exit status or equivalent evidence.
- Key conclusions require traceable evidence such as a file and symbol, command output, test result, or configuration value.
- Keep changes minimal. Do not opportunistically refactor unrelated code, add production dependencies, or alter architecture outside the approved task.
- A Luna agent must stop and report ambiguity, risk, or required work outside its authorization instead of expanding scope.
- Use `sol_escalation` only for qualified cross-system architecture, migration or irreversible change, security or permission risk, concurrency or consistency risk, major long-term tradeoffs, two failed Medium analyses, or credible production/data-loss risk. Do not use High for ordinary features, formatting, routine tests, simple bugs, search, or documentation.
- After Sol accepts a change and its commit succeeds, promptly push the current branch to `origin` (Gaoshou101/musicdl) so GitHub stays synchronized.
- Never force-push automatically, and never push failed or unaccepted work; report authentication, remote, rejection, or CI blockers accurately.
