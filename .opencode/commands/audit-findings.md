---
description: Check every gate finding has a decision-level disposition in DECISION.md
---

```bash
just audit-findings
```

Reads `F<n>` identifiers out of `.data-audit/VALIDATION.md` and reports whether each has a
disposition recorded in `.data-audit/DECISION.md`.

A finding with no decision-level record is the **R5 defect class**: the gate knows what it found, but
a reader auditing the pass can only recover the disposition by reading diffs. It is invisible in
review and trivial to check mechanically, which is why this is a command rather than a habit.

When it reports `MISSING`, record the disposition **with what was chosen and why**, including the
rejected options and any residual the choice accepts — not merely a commit reference. Add it under
the gate-closing section of `DECISION.md` and say plainly that the record is late and why the check
did not exist earlier.

`ACCEPTED, not fixed` is a valid disposition when a finding is coverage redundancy rather than a
defect; state the reasoning, because that is what distinguishes a decision from an omission.
