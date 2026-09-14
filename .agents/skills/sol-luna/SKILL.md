---
name: sol-luna
description: Run the repository's Astra-planned, Luna-executed, and Sol-reviewed workflow for implementation, investigation, testing, and review tasks that benefit from explicit delegation and evidence-based acceptance.
---

# Astra-Sol-Luna workflow

The coordinator preserves the user's intent, dispatches the stages, and reports only the result accepted by Sol. Astra owns planning, Luna performs concrete work, and Sol independently reviews the settled result.

## Runtime configuration

Use generic agents that permit explicit model and reasoning overrides. When full-history inheritance prevents overrides, use `fork_turns: "none"` and include a self-contained delegation contract.

| Stage | Model | Reasoning |
| --- | --- | --- |
| Plan and coordination | `gpt-6-astra` | Automatically selected; `medium` by default |
| Concrete work | `gpt-5.6-luna` | Always `max` |
| Independent review | `gpt-5.6-sol` | Automatically selected; `medium` by default |

Do not use fixed-model or fixed-effort roles when they cannot satisfy these settings. Record requested settings separately from observable runtime settings, and never claim a model, reasoning level, or provider mode that the tool did not expose or confirm.

Choose Astra and Sol effort independently for each stage:

- `low`: mechanical, localized, reversible work with explicit acceptance criteria and little uncertainty.
- `medium`: default for ordinary planning, investigation, implementation review, and test assessment, including initially unclear complexity.
- `high`: cross-system architecture, security or permissions, migrations, concurrency or consistency, irreversible operations, or credible production/data-loss risk.
- `xhigh` or `max`: only after High leaves material complexity unresolved; record the concrete reason.

Return to `medium` after the difficult decision is resolved. Luna remains at `max` regardless of task size.

## Fast mode

Fast mode is enabled by default as an execution policy:

- Keep one planning pass and one review pass unless new evidence requires revision.
- For every concrete-work wave, the main coordinator directly dispatches and uniformly coordinates exactly three useful, bounded Luna subagents; do not create filler work. Each subagent has a distinct package or read-only verification slice.
- Run the three subagents concurrently only when their scopes are independent. Keep shared-file or dependent changes serial and ordered under the main coordinator; on a four-slot host, the intended topology is main plus three Luna subagents.
- If the host cannot provide three eligible Luna subagents or slots, preserve only useful work within the observed limit and report the capability shortfall and actual topology; never claim three-way execution.
- Reuse compatible Luna subagents across waves for bounded corrections when practical, minimize repeated searches, and keep returns concise and evidence-heavy.
- Start verification as soon as its prerequisites settle, but never treat a launched command as a passing result.

If the runtime exposes a distinct provider `fast` or service-tier control, request it and record whether it was observed. Otherwise, apply only this workflow policy and do not claim that a provider-level fast mode was enabled.

## Capability preflight

Before dispatch, verify that the subagent tool actually supports all three model overrides, the required reasoning levels, and enough concurrency for the planned wave. A model shown elsewhere in the app does not prove subagent availability.

If a required model or reasoning control is unavailable, finish safe preparation and report the exact blocker. Do not silently substitute a model or reasoning level. If fewer than three useful bounded slices can be formed, or fewer than three eligible Luna subagents or slots are available, do not create filler; preserve useful work within the observed limit and report the capability shortfall and actual topology without claiming exact three-way execution. The main coordinator must still keep shared-file or dependent work serial.

## Route the work

1. The coordinator captures the requested outcome, permissions, existing changes, and repository constraints.
2. Dispatch Astra to inspect dependencies, risks, rollback boundaries, acceptance checks, and task ordering. Astra returns a bounded plan with Luna work packages and exclusive write ownership.
3. For each concrete-work wave, the main coordinator directly dispatches and uniformly coordinates exactly three useful, bounded Luna subagents according to the accepted dependency order. Use Luna for bounded exploration, reversible implementation, and independent tests or log analysis. Every Luna runs with reasoning `max`; run independent scopes concurrently and keep shared-file or dependent work serial. On a four-slot host, use main plus three Luna subagents.
4. Collect completed results, inspect the real changed-file scope, and route integration or correction work to one explicitly responsible Luna subagent within a subsequent wave that still has exactly three useful Luna subagents. Workers must preserve user and concurrent changes, and the main coordinator directly coordinates the wave.
5. After changes settle, dispatch Sol with the request, Astra plan, actual diff, changed-file list, and completed verification evidence. Sol independently returns `ACCEPT`, `REVISE`, or `BLOCKED` with traceable evidence.
6. On `REVISE`, issue bounded Luna correction work in a subsequent exactly-three-subagent wave and obtain fresh Sol review of the updated result. On `BLOCKED`, report the missing authority, capability, or evidence without widening scope.
7. Report only the result Sol accepted, including completed checks, skipped checks, remaining limitations, rollback, and observed model/reasoning settings.

## Delegation contract

Every delegated prompt must contain all eight fields:

1. **Task objective:** one bounded result plus assigned stage, model, and requested reasoning.
2. **Allowed scope:** exact readable/writable paths and exclusive ownership, or explicit read-only scope.
3. **Prohibited scope:** off-limits paths, actions, systems, and further delegation.
4. **Known context:** requirements, constraints, accepted evidence, dependencies, and other workers' boundaries.
5. **Completion criteria:** observable conditions for completion.
6. **Verification method:** exact commands or checks and expected result.
7. **Rollback method:** revert only this agent's changes while preserving existing and concurrent work; read-only tasks use “no changes.”
8. **Return format:** Conclusion; Modified files; Key changes; Commands executed with completed exit status; Test results; Unresolved issues; Risks and recommendations. Plans add dependency order and ownership; reviews add verdict and file/symbol evidence.

Workers do not spawn additional agents. If dispatch fails because inherited context cannot attach, retry once with `fork_turns: "none"` and the complete contract. If that fails, report the infrastructure blocker.

## Sol review gate

Sol reviews every settled result. Its reasoning follows the automatic selection policy above; do not use a fixed-effort role when it conflicts with the selected setting. A `REVISE` verdict routes implementation back to Luna, and Sol reviews the resulting fresh diff.

## Acceptance gate

Sol must inspect the substantive final diff and authorized changed-file scope, confirm required commands completed successfully, and distinguish requested runtime configuration from observed behavior. Fresh evidence must cover corrected files. Agent completion is not acceptance, and a started test is not a passing test.
