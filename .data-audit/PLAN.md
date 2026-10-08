# PLAN — data-pipeline audit pass, team `audit-pipeline-1008`

Pass date **2026-10-08**. Outcome type: **`IMPROVE-EXISTING`**. Chosen target:
**G-1 — wire the recorded order-book depth into the RL observation.**
Implementation committed at `745ff4d`. Validation verdict: **PASS**
(`.data-audit/VALIDATION.md`).

---

## 1. What was audited

Three parallel surveys (all read-only, all under `.data-audit/`):

- `AUDIT.md` — code map of the trading bot, ranked gaps. G-1 was the
  highest-directness item: the order-book depth was **fetched but not
  consumed**.
- `AUDIT-PIPELINE.md` — the RL data pipeline (`rl/*.py`, configs, tests) and
  its data-quality gaps.
- `AUDIT-SOURCES.md` — the data-sourcing/ops layer (`tools/`, `systemd/`,
  `justfile`, flake, Kraken API calls) and cross-category gaps.

`RESEARCH-{1,2,3}.md` surveyed libraries/APIs for the top three gaps;
`DECISION.md` picked one `(gap, improvement)` pair and pinned its constants
and directions (§7) for the gate.

**Why G-1 won.** `feature_groups` already ships with `"microstructure"`
enabled (`configs/default.yaml:53`), so the pipeline reserved width for a
reading it could never populate; the consumer
(`_add_microstructure_features` → `order_book_imbalance`,
`features.py:1030-1038`) was already coded; the data was already on disk. Only
delivery through an existing seam was missing. No new library beat ~30 lines
of pandas (RESEARCH-1 scored every candidate ≤3/10).

---

## 2. What was built (G-1)

- `_flatten_orderbook_records` (`kraken_trading_bot/rl/data.py:644`) — pure,
  reader-side reduction of the recorder's nested depth records to flat
  per-hour scalars: `timestamp←recorded_at`, `ticker←pair`, `bid_vol`/`ask_vol`
  (top-10 level-volume sums), `realized_spread_bps`. **Never** `spread`
  (reserved for the funding channel; depth merges last, so a depth `spread`
  would overwrite funding's — direction D-2).
- `bid_vol`/`ask_vol` added to `_SIGNAL_COLUMNS` (`features.py:129-130`) so the
  seam carries them to the frame, and to `_SIGNAL_BUILDER_INPUT_COLUMNS`
  (`features.py:161`) so `_add_signals_features` skips them — the raw volumes
  never reach the observation; only the derived `order_book_imbalance` does.
- 4th `_SIGNAL_CHANNELS` entry `orderbook_features_file` (`data.py:198-208`),
  threaded through both read legs (`data.py:1243` fetch, `data.py:1908` store)
  and all four callers: `train.py:254`, `backtest.py:567`, `export.py:181`,
  `paper_trade.py:315`. Off by default (`null`).
- `configs/default.yaml` — new `orderbook_features_file:` key + doc block.
- 12 offline hermetic tests in `tests/test_orderbook_depth_seam.py`.

**Validated (`.data-audit/VALIDATION.md`):** 756 passed / 0 failed; on the live
`signals/eth_usd_orderbook.jsonl` (114 records, 111 hours), with the channel
configured, `order_book_imbalance` is **present and non-constant** (112
distinct values, std 0.434) in `normalization.npz` `feature_names`, while
`bid_vol`/`ask_vol` are **absent**; width Δ vs the same config with the channel
off is **+1**.

---

## 3. Deferred (named, with the reason)

- **G-2 — trade tape + realized spread (`NEW-DATA-SOURCE`).** Kraken WS `trade`
  (live) + Binance aggTrades archive (seed); the wrapper already exists
  uncalled. Deferred because it needs a new recorder **and** a new seam column,
  the live leg is forward-only, and the only deep seed is Binance-USDT
  (cross-venue contamination of a USD book). It is the natural **second**
  feature once G-1's reader pattern exists — RESEARCH-1's Design C explicitly
  defers the generic reducer registry until the tape lands. **Next slice.**
- **G-6 — cross-exchange spot basis (`NEW-DATA-SOURCE`).** Keyless, one column
  `venue_basis_bps`; best done by extending the sibling `kraken-deep-history`
  with a `basis` subcommand (RESEARCH-3). Deferred: genuinely new sourcing (a
  producer + a second venue leg + a new config key) for a signal that is
  orthogonal rather than missing-and-coded. Cleanest `NEW-DATA-SOURCE` if the
  team wants a second slice.
- **G-3 — `microstructure` group degenerate** (`bid`/`ask` null on ~99 % of
  funding records). The cheap form (drop `spread`) shrinks width and adds no
  signal; the strong form reduces to G-1's depth feed. Not pursued separately.
- **G-4 — `stt_tilt`/`fng_index` inert.** Only removes dead columns, adds no
  information. Worth folding into the **same retrain** as G-1, not leading it.
- **G-9 — bar-count vs wall-clock feature windows.** High data-quality
  directness, large effort (time-aware windows over a reindexed grid), and
  only detected today, not fixed. Deferred.

---

## 4. Next steps

1. **Retrain is required and pending.** `models/` holds only `.gitkeep`, so the
   retrain cost today is zero — but no model has yet been trained on a
   +`order_book_imbalance` observation. `normalization.npz`'s `feature_names`
   is the width authority and `check_feature_width` (`features.py:483`) fails
   loudly at load time, so an old model cannot silently misalign.
2. **Enable the depth channel by default once coverage matures.** The live
   series is **forward-only**: the file covers ~1.1 % of a multi-year window
   (DECISION §7 C-7), so on the shipped default the column is 0.0 for the vast
   majority of bars. Keep `orderbook_features_file: null` until the recorder
   has accumulated enough contiguous hours to matter, then flip it on and
   retrain. Do not quote a backtest over a mostly-empty column as an effect.
3. **Measure before trusting.** When the channel is enabled, any claimed
   improvement must go through the repo's own `model-matrix` skill — multi-seed,
   out-of-sample — because a single paired run cannot separate a real effect
   from PPO seed noise. This pass proves **delivery**, not predictive value.
4. **G-2 next** if a second feature is wanted: the trade tape is the intended
   follow-on, and G-1's reader-side flattener is the pattern to copy.

---

PLAN COMPLETE
