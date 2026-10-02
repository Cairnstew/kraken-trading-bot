---
description: Deterministic pass close-out — verify in the pushed state, then push, then re-verify
---

```bash
just audit-closeout
```

Prints the pushed-state check and the ordered close-out steps. **Follow the order; two of the steps
exist because the reverse order produced a false result.**

1. `just audit-verify --prereg <c> --since <c>` — all four structural checks.
2. `git push`.
3. `just audit-verify` **again, in the pushed state.** A receipt from the working tree says nothing
   about what is on the remote. The 2026-10-02 pass verified in the tree and pushed afterwards; this
   ordering is the fix.
4. `just audit-findings` — every `F<n>` has a disposition.
5. `just audit-evidence` — nothing cited is `/tmp`-only.
6. Phase 8 self-improvement → append the RUN LOG entry as its own commit.
7. Shut down **every** teammate, then `team_cleanup`.

**Cleanup ordering is not cosmetic.** `team_cleanup` refuses while members are active, and its
safety net merges any unmerged branch. Verify after it that `git status` and `git diff` are empty —
a safety-net merge of already-landed work should introduce nothing, and if it does, that is a real
signal, not a formality.

**RUN LOG placement.** The append-only short entry goes in the `## RUN LOG` section of
`.opencode/commands/audit-pipeline.md`, pointing at the detailed per-pass `.data-audit/RUN-LOG.md`.
Keep evidence that a reviewer insisted on — a check proved inert by mutation, a reviewer's
self-correction — in the detail file, do not summarise it away.
