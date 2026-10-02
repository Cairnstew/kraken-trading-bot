---
description: Run the four structural gate checks and paste the receipt — one invocation, no hand-rolled re-derivation
---

Run the pass's structural verification **once**, as a receipt. Do not re-derive these checks by hand,
and do not write a throwaway probe script for them — that is what made the 2026-10-02 pass cost a
dozen script rewrites and made its evidence unreproducible.

Two reference commits, and they are usually **different**:

- `--prereg` — the commit that pre-registered the gate's threshold. `tools/model_matrix.py` must be
  **byte-identical** to it. If it moved, the pre-registration claim is void; stop and say so.
- `--since` — the last commit you believe is code-identical. The executable-AST check compares
  against this. Pointing it at the pre-registration commit reports `CHANGED` for files a later review
  round legitimately rewrote — a false alarm that looks exactly like a regression.

```bash
just audit-verify \
  --prereg <pre-registration-commit> \
  --since  <last-commit-you-believe-is-code-identical> \
  --frame  <cached-frame.parquet> \
  --expect-obs <sha> --expect-arr <sha>
```

Omit `--frame`/`--expect-*` if no frame is available (the check then prints a fingerprint rather than
an assertion, and says so). Add `--skip-suite ""`-style flag only via `--skip-suite` when the suite was
already run in this same pushed state.

**How to use the output.** Paste the receipt into the gate record **verbatim**. Do not summarise it
from intent: the 2026-10-02 pass described a docstring clause as "second" in a message while the file
said "first real", and a reviewer caught it only by reading the source. If a check reports
`SELF-TEST BROKEN`, the AST proof is not trustworthy — fix that before believing anything else it says.

Read `tools/width_check.py` for why the width check prints a FILE hash and an ARRAY hash separately.
They are different values for the same matrix; conflating them produces a confident false regression.
