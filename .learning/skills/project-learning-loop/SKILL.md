---
name: project-learning-loop
description: Convert verified user corrections, tested project discoveries, and successful non-trivial workflows into compact project memory or reusable procedures. Use after complex tasks, recovered failures, repeated feedback, or any explicit request to remember or change future agent behavior.
---

# Project Learning Loop

Run a short review after completing the user's actual task. Learning must never
delay the requested fix, replace testing, or expand the product scope.

## Closed Loop

1. Observe evidence from the request, corrections, code, tests, and final diff.
2. Extract only information likely to improve a future task.
3. Classify it using
   [references/storage-policy.md](references/storage-policy.md).
4. Verify it. Treat direct user statements and reproducible test results as
   verified; stage inferences instead of activating them.
5. Persist the smallest useful update. Replace conflicting or stale text rather
   than appending another version.
6. Validate limits, consistency, tests, and the absence of secrets.
7. Reuse the learning at the start of later tasks and refine it when new
   evidence disproves or narrows it.

## Review Triggers

Review learning when at least one condition holds:

- The user explicitly corrects the agent or says to remember a behavior.
- A successful task required five or more tool calls.
- The first approach failed and a reliable workaround was found.
- A non-obvious workflow is likely to recur.
- Existing memory or instructions were stale, conflicting, or incomplete.

Skip persistence for trivial facts, one-off state, raw logs, guesses, public
facts that are easy to rediscover, and content already documented elsewhere.

## Start Of Task

Read `.learning/USER.md` and `.learning/MEMORY.md`. Read
`.learning/PENDING.md` only when reviewing candidates. Load this skill when a
review trigger occurs. Search Git history when the exact earlier implementation
or decision matters; do not turn the memory files into a task diary.

## End Of Task

- Finish and test the requested work first.
- Apply verified learning in the same commit as the task when practical.
- Keep `AGENTS.md` and `CLAUDE.md` equivalent when project rules change.
- In the final response, add `학습 반영: ...` only when persistent learning
  actually changed. Do not announce an empty review.
- Never claim that model weights were trained. This loop improves file-backed
  context and procedures only.

