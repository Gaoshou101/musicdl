---
name: sol-luna
description: Run the repository's Astra-planned and Astra-dispatched, session-model-executed, Sol-reviewed workflow for implementation, investigation, testing, and review tasks that benefit from explicit delegation and evidence-based acceptance.
---

# Astra-Sol-Luna workflow

The coordinator preserves the user's intent, permissions, rollback decisions, and the final user-facing report. Astra owns planning and dispatch, the delegated subagents perform concrete work, and Sol independently reviews the settled result.

## Runtime configuration

Use generic agents that permit explicit model and reasoning overrides. When full-history inheritance prevents overrides, use `fork_turns: "none"` and include a self-contained delegation contract.

| Stage | Model | Reasoning |
| --- | --- | --- |
| Plan and dispatch | `gpt-6-astra` | Automatically selected; `medium` by default |
| Concrete work | The current session's main model | Inherited from that session's setting |
| Concrete-work fallback | `gpt-6-luna` | Fixed at `max`, with the fast service tier |
| Independent review | `gpt-6-sol` | Automatically selected; `medium` by default |

Rules:

- Astra and Sol each start at `medium` and raise their reasoning automatically with the complexity actually present in their stage.
- Concrete-work subagents run on the same model as the current session's main model, inheriting that session's reasoning setting. If the subagent tool cannot create a subagent on the session model, fall back to `gpt-6-luna` with reasoning fixed at `max` and request the fast service tier; record the fallback and its reason.
- Do not use fixed-model or fixed-effort roles when they cannot satisfy these settings.
- Record requested settings separately from observable runtime settings, and never claim a model, reasoning level, provider mode, or service tier that the tool did not expose or confirm.

### Reasoning ladder

Choose Astra and Sol effort independently for each stage:

- `low`: mechanical, localized, reversible work with explicit acceptance criteria and little uncertainty.
- `medium`: default for ordinary planning, dispatch decisions, investigation, implementation review, and test assessment, including initially unclear complexity.
- `high`: cross-system architecture, security or permissions, migrations, concurrency or consistency, irreversible operations, or credible production/data-loss risk.
- `xhigh` or `max`: only after High leaves material complexity unresolved; record the concrete reason.

Return to `medium` after the difficult decision is resolved.

## Fan-out decision

Astra decides for each task whether to fan out at all and how many concrete-work subagents to use. The decision follows the work rather than a fixed topology:

- Zero subagents: the task is small, single-threaded, or confined to one file or code area. The coordinator or Astra performs it directly, and Sol still reviews the settled result.
- One subagent: exactly one bounded slice dominates the work, so parallelism would add only coordination cost.
- Two or three subagents: only when that many genuinely independent, useful slices exist and the host can run them concurrently.

Constraints:

- Never create filler work to reach a target count, and never dispatch a slice that has no independent value.
- Treat three concurrent subagents as the default ceiling on a four-slot host (coordinator plus three); exceed it only when the runtime reports more capacity.
- Run independent read-only or disjoint-path slices concurrently. Run dependent work, or work that touches the same file or code region, serially.
- Report the actual topology used, including any capability shortfall, rather than describing a fan-out that did not happen.

## Fast mode

Fast mode is an execution policy, and this project also requests the provider's fast service tier where it is available:

- Keep one planning pass and one review pass unless new evidence requires revision.
- Ask Astra for a single dispatch decision per wave instead of re-planning between subagents.
- Reuse compatible subagents across waves for bounded corrections when practical, minimize repeated searches, and keep returns concise and evidence-heavy.
- Start verification as soon as its prerequisites settle, but never treat a launched command as a passing result.

If the runtime exposes a distinct provider `fast` or service-tier control, request it and record whether it was observed. Otherwise, apply only this workflow policy and do not claim that a provider-level fast mode was enabled.

## Capability preflight

Before dispatch, verify that the subagent tool actually supports the required model override (the session main model, or `gpt-6-luna` at `max` as the fallback), the required reasoning levels, and enough concurrency for the planned wave. A model shown elsewhere in the app does not prove subagent availability.

If a required model or reasoning control is unavailable, finish safe preparation and report the exact blocker. Do not silently substitute a model or reasoning level. If the fan-out decision cannot be honored within the observed limits, keep only the useful slices that do fit, hold shared-file or dependent work serial, and report the capability shortfall and actual topology.

## Route the work

1. The coordinator captures the requested outcome, permissions, existing changes, and repository constraints.
2. Dispatch Astra with model `gpt-6-astra`. Astra inspects dependencies, risks, rollback boundaries, acceptance checks, and ordering; decides whether to fan out and how many subagents to use; defines bounded work packages with exclusive write ownership; and dispatches those subagents.
3. Astra's subagents execute the concrete work: bounded exploration, reversible implementation, and independent tests or log analysis. Independent scopes run concurrently; shared-file or dependent work stays serial. If the runtime denies dispatch to Astra, Astra returns the complete dispatch contract and the coordinator executes it verbatim.
4. Collect completed results, inspect the real changed-file scope, and route integration or correction work to one explicitly responsible subagent in a subsequent wave. Workers preserve user and concurrent changes.
5. After changes settle, dispatch Sol with model `gpt-6-sol`, providing the request, Astra's plan, the actual diff, the changed-file list, and completed verification evidence. Sol independently returns `ACCEPT`, `REVISE`, or `BLOCKED` with traceable evidence.
6. On `REVISE`, issue bounded correction work in a subsequent wave and obtain fresh Sol review of the updated result. On `BLOCKED`, report the missing authority, capability, or evidence without widening scope.
7. Report only the result Sol accepted, including completed checks, skipped checks, remaining limitations, rollback, and observed model and reasoning settings.

## Delegation contract

Every delegated prompt must contain all eight fields:

1. **Task objective:** one bounded result plus assigned stage, model, and requested reasoning.
2. **Allowed scope:** exact readable and writable paths with exclusive ownership, or an explicit read-only scope.
3. **Prohibited scope:** off-limits paths, actions, systems, and further delegation.
4. **Known context:** requirements, constraints, accepted evidence, dependencies, and other workers' boundaries.
5. **Completion criteria:** observable conditions for completion.
6. **Verification method:** exact commands or checks and the expected result.
7. **Rollback method:** revert only this agent's changes while preserving existing and concurrent work; read-only tasks use no changes.
8. **Return format:** Conclusion; Modified files; Key changes; Commands executed with completed exit status; Test results; Unresolved issues; Risks and recommendations. Plans add the fan-out decision, dependency order, and ownership; reviews add a verdict and file or symbol evidence.

Workers do not spawn additional agents; Astra owns fan-out. If dispatch fails because inherited context cannot attach, retry once with `fork_turns: "none"` and the complete contract. If that fails, report the infrastructure blocker.

## Sol review gate

Sol reviews every settled result. Its reasoning follows the automatic selection policy above; do not use a fixed-effort role when it conflicts with the selected setting. A `REVISE` verdict routes implementation back to a subagent, and Sol reviews the resulting fresh diff.

## Acceptance gate

Sol must inspect the substantive final diff and the authorized changed-file scope, confirm required commands completed successfully, and distinguish requested runtime configuration from observed behavior. Fresh evidence must cover corrected files. Agent completion is not acceptance, and a started test is not a passing test.
