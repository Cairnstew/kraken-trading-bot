---
description: Re-derive the cost-aware CAND-5 dispersion figures from per-seed records using the shipped estimator
---

Re-derive the cost-aware gate figures from the per-seed backtest records, so a published result table
is reproducible from committed code instead of a scratch script in `/tmp`.

```bash
just cost-aware-replay \
  --records        /tmp/<run>/cost \
  --control-records /tmp/<run>/ctrl \
  --cell-live  <id1>:<seed1>,<id2>:<seed2>,<id3>:<seed3> \
  --cell-store <id1>:<seed1>,<id2>:<seed2>,<id3>:<seed3>
```

**Annotate every cell with its seed (`id:seed`).** Measured limitation: these backtest records all
carry the *same* `seed` field (the trainer's default), so they cannot be used to pair the two arms.
Unannotated, pairing is asserted by list position and printed as such — which means a mis-ordered
`--cells` silently computes a cost ratio across mismatched seeds. Annotated, pairing is verified and
a mismatch aborts.

The driver (`tools/cost_aware_gate.py`) **imports** `DISPERSION_RATIO_THRESHOLD`, `summarize`,
`pooled_within_spread` and `dispersion_verdict` from `tools/model_matrix.py` and chooses nothing
itself. Keep it that way. Never add a threshold, an estimator, or a duplicate of either — a second
source of truth is exactly how a pre-registered gate stops being trustworthy. `tools/model_matrix.py`
must stay byte-identical to the pre-registration commit; that is why this driver is a separate file
rather than a new subcommand.

The cost-ratio line prints **two** proxies and says neither is the true ratio, because the records
carry no notional. Keep both, keep the caveat about the `+1800%` seed, and do not collapse them into
one number.
