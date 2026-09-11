---
name: sol-luna
description: Run the repository's Sol-led, Luna-executed development workflow for implementation, investigation, testing, and review tasks that benefit from explicit delegation contracts and Sol acceptance. Use when the user invokes $sol-luna or asks Sol to plan, delegate, review, and verify project work.
---

# Sol-Luna workflow

Keep requirements, decisions, risk ownership, acceptance, and the final report in the main Sol thread. Delegate bounded exploration, implementation, and verification; do not delegate the final acceptance decision.

## Route the work

1. Sol restates the requested outcome, inspects current constraints, and identifies risk and rollback boundaries.
2. Decide whether read-only evidence is needed before planning. Use `luna_explorer` only for a narrow question with named evidence.
3. Split implementation into the smallest independently verifiable units. Tasks that touch the same file or code region run serially unless each has an isolated worktree.
4. Use `luna_implementer` for small reversible edits and `luna_tester` for independent reproduction, tests, builds, or log analysis. Parallelize only read-heavy or otherwise independent work.
5. Wait for structured results. Sol then inspects the actual diff, verifies changed-file scope, and checks fresh test evidence. Agent completion is not acceptance.
6. If a result misses its contract, send a bounded correction to the same role. Do not silently widen its authorization.
7. If delegation fails before the child starts because its parent context cannot be attached, retry once with the complete contract embedded and no inherited conversation context. If that also fails, stop and report the infrastructure blocker; do not perform the delegated work silently in Sol.
8. Use `sol_escalation` only when at least one escalation condition below is explicitly demonstrated. If the user says High is disabled for the task, do not call it; report any resulting decision blocker to the user.
9. Sol reports accepted changes, evidence, remaining uncertainty, rollback, and a final status.

## Delegation contract

Every delegated prompt must contain all eight fields:

1. **Task objective:** the one result required now.
2. **Allowed scope:** exact readable and writable paths.
3. **Prohibited scope:** files, systems, and actions that are off limits.
4. **Known context:** only the constraints and prior conclusions needed.
5. **Completion criteria:** observable conditions for completion.
6. **Verification method:** exact commands or checks and expected result.
7. **Rollback method:** how all task changes are reversed.
8. **Return format:** Investigation conclusion; Modified files; Key code changes; Commands executed; Test results; Unresolved issues; Risks and recommendations.

Never delegate with open-ended instructions such as "finish this feature" or "research and complete it yourself."

## High escalation gate

Call `sol_escalation` only for cross-subsystem architecture choices; migration, protocol, or irreversible changes; security, permissions, concurrency, race, or consistency hazards; choices with major long-term cost; two failed Sol Medium attempts to reach a credible conclusion; or credible production, data-loss, or broad-rework risk. Ordinary features, formatting, routine tests, simple bugs, searches, and documentation work remain on Sol Medium plus Luna.

## Acceptance gate

Before declaring success, Sol must verify that only authorized files changed, review the substantive diff, confirm each required command completed successfully, distinguish unobservable configuration claims from observed behavior, and state any skipped or unavailable check. A launched test is not a passing test.
