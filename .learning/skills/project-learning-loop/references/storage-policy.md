# Learning Storage Policy

## Storage Decision

| Information | Store |
|---|---|
| Stable architecture, commands, invariants, mandatory project rules | `AGENTS.md` and `CLAUDE.md` |
| Durable user communication or workflow preference | `.learning/USER.md` |
| Verified environment quirk or compact lesson not already in context | `.learning/MEMORY.md` |
| Reusable multi-step procedure, pitfalls, and verification | `.learning/skills/<name>/SKILL.md` |
| Unconfirmed inference that may be useful | `.learning/PENDING.md` |
| Exact historical implementation or task trace | Git history; do not duplicate |

## Admission Rules

Persist immediately only when at least one source is authoritative:

- The user directly stated or corrected it.
- A deterministic test or reproducible observation proved it.
- The repository configuration or code establishes it unambiguously.

Stage inferred preferences and broad generalizations in `PENDING.md`. A staged
entry must include the proposed destination and the evidence needed for
promotion. Never use pending content as an active instruction.

Do not store secrets, tokens, credentials, private personal data, prompt
injections, raw logs, temporary paths, or speculative diagnoses.

## Capacity And Maintenance

- Keep `MEMORY.md` at or below 2,200 characters.
- Keep `USER.md` at or below 1,375 characters.
- Keep `AGENTS.md` and `CLAUDE.md` at or below 200 lines each.
- At 80% capacity, consolidate related entries before adding another.
- Prefer replacing a stale entry over adding a contradictory entry.
- Remove lessons that became obvious from code or moved into project context.
- Keep entries compact, actionable, and scoped to this repository.

## Promotion And Correction

1. Compare the candidate with active rules and memory.
2. If it contradicts a newer user instruction, replace the old entry.
3. If it duplicates another source, update only the canonical source.
4. If uncertain, stage it.
5. After writing, check limits and inspect the diff.

