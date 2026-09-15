---
name: agent-loop
description: Coordinate a pool of Luna or Terra Codex workers under Astra supervision in a plan, execute, review, and repair loop. Use for requests for an agent swarm, supervised worker pool, or 10–15 agents working through a coding task.
---

# Agent Loop

Complete the user's task with Astra supervising parallel Luna or Terra workers.
Use Codex's native collaboration tools; this skill supplies the workflow, not a
background service or a way to change runtime limits.

Example invocations:

```text
$agent-loop Implement <feature> with 12 Luna workers supervised by Astra.
$agent-loop Fix <problem> with 15 Terra workers; review and repair until checks pass.
```

If invoked without a concrete task, ask what the pool should work on before
spawning. Creating or editing this skill alone does not start a worker pool.

## Team and capacity

Default to a target of 12 workers, accepting the user's requested count and model.
Use fewer when the task has fewer independent work units; do not invent work to
fill the pool. Report the requested count and the actual pool size separately.

| Responsibility | Model | Reasoning effort |
| --- | --- | --- |
| Planning, monitoring, review, difficult repairs | `gpt-6-astra` | `high` |
| Narrow changes and focused checks | `gpt-5.6-luna` | `medium` |
| Changes requiring broader code reasoning | `gpt-5.6-terra` | `high` |

Use Luna by default for narrow work and Terra for more involved implementation,
unless the user chooses one worker model for the whole pool. These are explicit
model selections for this workflow. Check the available model list before spawning;
if a required model is unavailable, report it and ask for a replacement instead
of silently claiming another model is Astra, Luna, or Terra.

If the primary agent is known to be Astra, it supervises directly. Otherwise,
spawn one Astra supervisor and have the primary agent dispatch workers as its
siblings. The supervisor owns planning and review; the primary owns scheduling
and integration. Workers and the supervisor must not spawn their own agents.

Inspect the runtime's capacity and existing agents before filling the pool.
Account for the primary, supervisor, and unrelated agents according to the
runtime's counting rules. Never interrupt unrelated agents to reclaim capacity.
For example, a limit of four agents including the primary permits three workers
with an Astra primary, or two workers with a separate Astra supervisor.

Queue the remaining work and reuse available workers in waves. A completed or
interrupted agent may still occupy a slot: use follow-up tasks rather than
assuming a new spawn will fit. A skill cannot raise a runtime limit. Do not change
Codex configuration or start extra CLI processes to bypass it. If there is no
capacity for both supervision and a worker, explain the limit before proceeding.

## Work loop

1. **Plan.** Read the applicable `AGENTS.md` and inspect the current changes.
   Have Astra define observable acceptance criteria and divide the work into
   bounded tasks with dependencies, file ownership, and relevant checks.
   Keep a compact ledger in the conversation: task, owner/model, dependencies,
   status (`queued`, `running`, `review`, `repair`, `done`, `blocked`), and evidence.

2. **Dispatch.** Fill available slots only with tasks whose dependencies are
   ready. Give each worker the objective, relevant paths and context, exclusive
   write ownership or a read-only role, expected output, and checks. All agents
   share the checkout: tell each worker it is not alone, must preserve others'
   edits, and must coordinate changes outside its ownership. Serialize edits to
   shared files. Keep integration files with one owner. Workers report back when
   their bounded assignment is complete; they do not run this skill recursively.

3. **Monitor.** While workers run, perform useful coordination or integration
   outside their ownership. Send blockers, interface changes, and completed work
   to Astra. Use messages for active agents and follow-up tasks for idle agents.
   Wait for completion notifications when no useful independent work remains,
   using waits no longer than 60 seconds and giving concise progress updates.
   Interrupt a worker only to stop conflicting, obsolete, or explicitly cancelled
   work; inactivity alone does not prove it is stuck.

4. **Review.** Astra inspects the actual changes and check results against the
   acceptance criteria. A worker's completion claim alone is insufficient.
   Review completed tasks as they arrive, and keep unrelated ready work moving.
   Return either accepted work or concrete repair instructions with evidence.
   Reviewers remain read-only while workers own the affected files.

5. **Repair.** Send a focused follow-up to the same worker. After two unsuccessful
   repairs of the same issue, have Astra revise the task or take over that repair
   once the worker has stopped editing. Use a Terra replacement for Luna only
   when the chosen worker policy and capacity permit a new agent; follow-up
   messages do not change an existing agent's model. Record an external blocker
   when progress needs missing access or information, and continue independent
   work. Avoid repeating the same failed instruction indefinitely.

6. **Integrate and finish.** With affected writers finished, inspect the combined
   diff and run the checks required for the complete change. Route new failures
   through review and repair. Finish when acceptance criteria and required checks
   pass, the user stops the run, an explicit budget is reached, or remaining work
   depends on an external blocker. Report incomplete work honestly. Do not add
   new scope just to keep agents busy, or repeat successful checks without a reason.

## Native tool usage

Use the collaboration tools exposed by the session. With `collaboration.spawn_agent`,
set `fork_turns: "none"` (or a supported bounded history) when selecting a model;
a full-history fork inherits the parent's model and does not accept overrides.
Pass all needed context in `message` when using no history. For example:

```json
{
  "task_name": "worker_01",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "medium",
  "fork_turns": "none",
  "message": "Implement TASK. Context: CONTEXT. Own only PATHS. Acceptance criteria: CRITERIA. Run CHECKS. You are not alone in the codebase: preserve others' edits and coordinate changes outside your ownership. Do not spawn agents. Return changed files, check commands and results, and any blockers."
}
```

Use `list_agents` for inventory, `send_message` for active agents,
`followup_task` to restart an idle agent with its next assignment or repair,
`wait_agent` for notifications, and `interrupt_agent` for targeted cancellation.
Call these tools directly; they are not available inside `functions.exec`.
Adapt to the actual tool schema when a different Codex runtime exposes equivalents.
If native delegation is unavailable, report that the pool cannot run in this session.

Keep the final report brief: completed outcome, actual models and peak worker
count, validation, and unresolved blockers. Preserve the user's existing action
authorization; a supervisor's technical acceptance does not grant permission for
unrequested publishing, messaging, or destructive actions.
