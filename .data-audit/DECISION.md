# Decision — 2026-10-01

Overwrites the prior-pass DECISION.md (git `18343ce`), which chose Candidate 1 =
make the three signal seams sound (A1–A4) — **LANDED** as `5951f72`/`1e404ba`,
GATE: PASS (`VALIDATION.md` §5). This pass is decided on the fresh, on-disk,
authoritative `AUDIT.md` (`ca360a8`), the three researcher files it derives from
(`RESEARCH-1/2/3.md`), the lead-assembled `RESEARCH.md`, `VALIDATION.md`, and
`PLAN.md` — all read in full first. An earlier AUDIT.md draft with Rank-1–4
numbering was superseded; the ranked substance is identical, and this decision
uses the revised gap numbering.

---

## 1. OUTCOME TYPE

**IMPROVE-EXISTING.**

Chosen on AUDIT.md evidence, not by default. The authoritative audit's own
ranking makes the case: Gap 1 scores **5/5 directness / 1/5 difficulty**, is
fully in-repo, needs **zero new repo, dependency, or API call**, and is the
natural continuation of the pass-2 sequence the prior DECISION.md already
championed ("this repo's data gap is not 'we need another source'"). The audit
is explicit that the IMPROVE-EXISTING / dead-machinery class is:

> *"the highest-directness class; explicitly the class prior `DECISION.md` §1
> says must not be pre-filtered away"* — AUDIT.md Gap 1 heading.

The decision-shaping evidence jointly points the same way:

- **Gap 1's six/five on-disk columns** (`vwap`, `count` + `vol24h`,
  `funding_rate_prediction`, and the prior `bid`/`ask` → `spread`) are already
  in the parquet/JSONL every read already parses. Zero new bytes to acquire.
- **The microstructure recorder (the NEW-DATA-SOURCE candidate) is keyless and
  ~100× under rate limits** (RESEARCH-3 §2) — legitimate, but it is **3/5
  difficulty, a new sibling repo, a recorder process, and is gated on the
  scheduler that does not exist yet** (Gap 2). Its own researcher frames it as
  the *fallback*: RESEARCH-3 §4 — *"In-repo (option i) ... is strictly inferior
  on tape coverage but is ~30 lines and no infra"* — and Gap 1 is strictly
  cheaper with higher directness.
- **Substantive overlap**: RESEARCH-1 §1 shows `volume_per_trade` *is* the
  bar-scale analogue of the tape's `mean_trade_size`, and `vwap_dev` overlaps
  the tape's `vwap_pressure` (RESEARCH-3 §3 ii). The recorder's genuinely-new
  scalars (taker side split, `realized_spread_bps`) do not clear the bar over
  six columns that are literally free.
- **`spread` must stay single-writer** (decision-shaping note, RESEARCH-3
  §4.4): Gap 1 activates `spread` from funding `bid`/`ask`; a future recorder
  must use `realized_spread_bps`. Complementary, not competing — which is why
  the recorder is the **runner-up**, not a competitor this pass.

**No new repo. No naming step. No submodule.** Phase 4 is **4B — implement in
the existing target** (`kraken-trading-bot`), plus one *invoke-only* use of the
existing `kraken-funding-rates` sibling CLI to produce the one JSONL that turns
the funding half of Gap 1 on. The sibling is not modified.

## 2. CHOSEN TARGET (one)

**Gap 1 — activate the on-disk columns the feature pipeline throws away**, as a
coherent slice that is honest about Gap 1's one caveat. AUDIT.md states that
caveat plainly: *"because every `*_features_file` key is `null`, this ships 6
columns that still produce 0 until a funding file is configured — it needs Gap
5 to actually turn on."* RESEARCH.md's cross-file note #1 therefore defines the
deliverable exactly:

> *"a coherent slice is Gap 1 columns + Gap 5 activation (at least a funding
> file via the sibling CLI under a systemd.user timer, i.e. one slice of Gap
> 2)."*

So the single outcome — *the model's observation actually contains the columns
that already exist on disk, provably width-guarded, in production* — is
delivered as: **Gap 1 widening (49 → 55) + the mandatory `n_features` width
guard (the Gap 4 component any widening must ship with) + minimal Gap 5
activation of the funding file under one `systemd.user` timer (one slice of Gap
2).** That is one outcome with its named set of in-scope landing points, not
three half-built targets; every element is a precondition named by Gap 1's own
evidence.

### 2.1 The committed column set — 49 → 55

Measured base today: **49** (RESEARCH-1 §0, ran the real pipeline:
price=10, technical=33, volume=6, microstructure=0, signals=0). Committed
delta **+6 → 55**, per RESEARCH-1 §6's recommended minimal set (items 1+3+4).

| # | Column | Source bytes | Produced at | Read per ticker at | Zero-config? |
|---|---|---|---|---|---|
| 1 | `vwap_dev` = `close/vwap − 1` | OHLCV frame (`_OHLCV_COLUMNS`, `data.py:58`) | derive at seam (`data.py:623`/`:779-787`/`:929-940`) | `_SIGNAL_COLUMNS` `features.py:42-54`; `_add_signals_features` `features.py:434-436` | **yes — works immediately, no config, no file** |
| 2 | `trade_count_zscore_20` | `count` parse `data.py:651` | same seam | same | **yes** |
| 3 | `volume_per_trade` = `volume/count` | `volume` + `count` parse | same seam | same | **yes** |
| 4 | `funding_rate_prediction` | funding JSONL (`kraken_funding_rates/models.py:58`) | sibling CLI, hour-floored | merge allow-list `data.py:420-424` | needs funding file (activation) |
| 5 | `vol24h` | funding JSONL (`models.py:65`) | same | same | needs funding file |
| 6 | `spread` (clean +1) | funding `bid`/`ask` (`models.py:63,64`) → `_add_microstructure_features` `features.py:408-413` | same | same; builder already test-covered (`tests/test_rl_environment.py:205-215`) | needs funding file |

**Excluded from the committed set, as the borderline internal runner-up:**
`mark_price`, `index_price`. They are 2 lines on the same tuple and ride the
same retrain, but RESEARCH-1 §3 makes the deeper, measured call: the funding
sibling computes `basis = (mark_price − index_price) / index_price`
(confirmed at `kraken_funding_rates/models.py:30,101`), and `basis` is already
merged. The audit's counter-claim — mark-vs-OHLCV-`close` is a genuinely
different (spot-side) number — is real but marginal. **Gate number: 55.
Integrator direction: if the interface budget permits, `mark_price` and
`index_price` may ride the same `_SIGNAL_COLUMNS` edit for the same retrain cost
(→ 57), but they are not required for the gate and default to excluded.**
Naming them keeps the width accounting unambiguous and the decision single.

**Clean-vs-naive accounting is fixed at clean:** whitelist `bid`/`ask` as
*builder inputs* and let `_add_microstructure_features` emit `spread`; exclude
`bid`/`ask` from raw `signals` pass-through the way `_SIGNAL_FRESHNESS_COLUMNS`
is excluded, so the net is **+1 (`spread`), not +3** (RESEARCH-1 §3 "clean +1").
No raw dollar-price columns in the observation.

**`order_book_imbalance` (+1 → 56) is explicitly NOT in this slice.** It needs
the `bid_vol`/`ask_vol` producer, which requires the engine recorder (Gap 3
territory, RESEARCH-1 §2/§6 item 2, "only if the engine loop is the intended
deployment"). Deferred with Gap 3.

### 2.2 The mandatory guard (Gap 4 component, lands WITH the widening)

Decision-shaping note (and RESEARCH-1 §1.1, RESEARCH-2 §2): any widening **must**
ship with `n_features` provenance, or the six on-disk 49-wide models silently
run column-less. `paper_trade.py:327-340`'s `_validate_observation` is
**tautological for this threat** — both sides derive from the same loaded
`NormalizationStats`, and `NormalizationStats.normalize` (`features.py:138-144`)
silently drops columns not in `feature_names`. `backtest.py` has **no guard at
all**. In-scope landing points:

- `train.py:238-242` — write `n_features` (`env.observation_space.shape[0]`,
  RESEARCH-2 §2.2) into the model's `config.yaml`.
- `registry.py:60-78` `config_summary()` — surface `n_features` (and the
  `data_window`/`feature_fingerprint` fields are SPEC'd in RESEARCH-2 §2.2 but
  are **deferred** — not required to make Gap 1 safe; only `n_features` is in
  this slice).
- Replace the tautological guard with a non-self-referential width assertion:
  `feature_names` read from `normalization.npz` (`features.py:94,117`, already
  round-tripped) vs the live pipeline's column set, at load.
- **All 6 on-disk `models/{ETH,SOL,XRP}_USD/*` artifacts must be deleted and
  retrained at 55** — they are null-signal scratch artifacts (`PLAN.md` §5 item
  4), so there is no backtest/reproducibility burden; a future widening is what
  the guard now makes loud.

### 2.3 The minimal activation (Gap 5 / one slice of Gap 2)

- `configs/default.yaml:65` — set `funding_features_file:` to a real path,
  populated by `kraken-funding-rates pull --pair ETH/USD --output <path>
  --append` (idempotent by `(ticker, hour)`; RESEARCH-2 §4.1). One-off pull
  generates the history for a training window; the timer keeps it fresh for
  live paper-trade so `signal_observed` is non-zero in production.
- `signal_max_age_hours`: funding settles ~8-hourly, and at the `null`→1 h
  default a funding reading covers only **76/721 bars** vs **301/721** at 12
  (`VALIDATION.md` §4). Activation therefore sets `signal_max_age_hours: 12`
  in the funding-bearing config. The per-source-bounds schema change is the
  filed Gap-2/PLAN follow-up, not this slice.
- **One timer only**: a `systemd.user` timer+service pair in this repo
  (`nix/module.nix` `systemd.user.*` section, or a plain unit under
  `~/.config/systemd/user/`), `OnCalendar` hourly or 3×/day, invoking the
  funding CLI. That is **not** Gap 2's full fix (news/social timers, flake
  inputs, market-data store timer, and the `kraken-market-data` ExecStart bug
  all stay deferred) — it is the single unit without which Gap 1's funding
  columns are silent, per Gap 1's own caveat.

## 3. LIBRARY(IES)

**None. Stdlib + numpy + the pandas already a hard dependency**
(`flake.nix:58-64`). The three OHLCV derivations are two-line vectorized
pandas operations on the frame the pipeline already holds; the funding columns
are raw z-scored pass-throughs. RESEARCH-1/2/3 all independently returned
"no new deps"; there is no library-shaped win on this board (prior DECISION
§3 consensus stands: duckdb/polars/dvc solve storage/parallelism/versioning,
none of which this frame size needs).

## 4. IMPROVEMENT SCOPE (Phase 4B)

**Touched repo: this one — `/home/seanc/Projects/kraken-trading-bot.`** The
`kraken-funding-rates` sibling is *invoked* (its CLI already exists and emits
hour-floored JSONL) but **not modified**. In-scope files/units:

| Unit | File:line (commit `ca360a8`) | Change |
|---|---|---|
| Allow-list | `kraken_trading_bot/rl/features.py:42-54` `_SIGNAL_COLUMNS` | +6 names (`vwap_dev`, `trade_count_zscore_20`, `volume_per_trade`, `funding_rate_prediction`, `vol24h`, `spread`); `bid`/`ask` as builder inputs excluded from raw pass-through |
| Seam derive | `rl/data.py:623` (`candles_to_dataframe`) or `:775-787`/`:929-940` (post-merge, both legs) | derive the three OHLCV scalars, presence-gated like `_add_microstructure_features` (`features.py:408-418`); no `_OHLCV_COLUMNS`-required change |
| Micro builder | `rl/features.py:405-418` `_add_microstructure_features` | already emits `spread` from `{bid,ask}` — no change required; verify covered by `tests/test_rl_environment.py:205-215` |
| Width guard | `rl/registry.py:60-78`, `rl/train.py:238-242`, `rl/paper_trade.py:327-340`, `rl/backtest.py` | `n_features` in config_summary + write; non-tautological assert (`npz` `feature_names` vs pipeline columns) at load |
| Activation | `configs/default.yaml:65` (+ `configs/deep-history.example.yaml:56-58`) | `funding_features_file` path; `signal_max_age_hours: 12` |
| Timer | `nix/module.nix` (`systemd.user.*`) or `~/.config/systemd/user/` | one funding-pull unit (Option A, RESEARCH-2 §4.2) |
| Tests | `tests/test_rl_data_store.py`, `tests/test_rl_environment.py`, `tests/test_rl_signal_config_wiring.py` | RESEARCH-1 §6 verification hooks §7: `{vwap,count}` compute assert; `_SIGNAL_COLUMNS` member round-trip through `merge_extra_features`; width probe `compute().shape[1] == 55` with all inputs, `== 49` when new inputs absent (presence-gated); the 6 funding/OHLCV names reach the observation |

**Explicitly NOT in this slice** (stays deferred, with reasons): `until` client
fix (~8 LOC, changes no column, belongs to data-window recording — RESEARCH
note 5); full Gap 2 scheduler + flake inputs for the other two signal CLIs;
Gap 3 engine recorder / `order_book_imbalance`; full Gap 4 provenance suite;
`mark_price`/`index_price` (gate-optional); on-chain / macro / academic /
new-text-news (re-verified and still rejected — AUDIT.md "Categories
deliberately NOT ranked", unchanged from DECISION §6.6-6.8).

## 5. INTEGRATION SKETCH (one paragraph)

The output contract is unchanged in shape — a per-(ticker, bar) OHLCV frame
whose per-ticker observation gains 6 named scalars, delivered through the
existing, already-sound seam (`merge_extra_features`, repaired `5951f72`): the
three OHLCV-derived columns (`vwap_dev`, `trade_count_zscore_20`,
`volume_per_trade`) are computed in `read_ohlc_dataframe`'s return path on both
the live leg (`data.py:779-787`) and the store leg (`data.py:929-940`), so all
four consumers (`train.py:188`, `backtest.py:138`, `export.py:166`,
`paper_trade.py:294`) inherit them with zero config the moment the code lands;
the three funding columns (`funding_rate_prediction`, `vol24h`, `spread`) enter
through the same `funding_features_file` key every consumer already threads,
fed by one `systemd.user` timer running the `kraken-funding-rates` CLI. The
widened frame flows unchanged through `FeaturePipeline.compute`
(`features.py:280`) — the micro group (`:405`) turns `{bid,ask}` into `spread`,
the signals group (`:420`) copies the new `_SIGNAL_COLUMNS` members — then
`_raw_feature_array` (`environment.py:445`) → `_observe` (`:367`) → `PPO`. Each
new column's value is per-ticker-normalized by the existing `NormalizationStats`
(per-ticker, z-scored — RESEARCH-1's note that `vol24h` is a slow-moving level
is handled by z-scoring), and the pipeline is retrained at width 55, with
`n_features` recorded in the registry so `scan_model`/`cli.py models` shows any
stale-width artifact instead of silently running a column-less policy.

## 6. RUNNER-UPS (and exactly why each lost)

1. **NEW-DATA-SOURCE — the `kraken-microstructure` trade-tape/spread recorder
   (Gap 3, the leading alternative).** Legitimate: keyless, both wrappers
   (`recent_trades`/`spread`) return `(rows, last)` exactly like `_page_candles`
   (RESEARCH-3 §1d), ~100× under rate limits at 15-60 s (RESEARCH-3 §2),
   feed-directness 4/5. **It lost on directness-per-cost and sequencing.**
   Gap 1 is 5/5 directness / 1/5 difficulty with zero new repo/dep/API;
   the recorder is 3/5 difficulty, a new sibling project with a `_meta.json`
   opaque-cursor sidecar, and — decisively — **it is gated on the scheduler
   that does not exist yet (Gap 2)**: the tape only exists if someone listens,
   and the same timer Gap 1's slice needs for one funding CLI is the
   *precondition* the recorder needs for a 15-60 s poller. Its headline scalars
   also overlap Gap 1's free ones (`volume_per_trade` vs `mean_trade_size`;
   `vwap_dev` vs `vwap_pressure`; RESEARCH-1 §1). The `spread` single-writer
   rule means it must use `realized_spread_bps` regardless — compatible but not
   additive. Correct ordering: this pass ships the free columns and the first
   timer; the recorder is the right **next** NEW-DATA-SOURCE once the scheduler
   and Gap 5 activation actually exist (i.e. as a follow-up pass).
2. **Gap 5 alone (activation).** Directness 4/5 but difficulty 1/5 and no new
   columns — it is the *precondition* included in this slice as §2.3, not an
   independent target; there is nothing to activate that is more load-bearing
   than the funding file the committed columns need.
3. **Gap 2 in full (schedulers + flake inputs for all three signal CLIs).**
   Directness 2/5, difficulty 2/5, and it adds no column. The full build (three
   `systemd.user`/system timers + `git+https` flake inputs, RESEARCH-2 §4
   options A→C) is deferred; only Option A's single funding unit ships here
   because Gap 1's caveat names it.
4. **Gap 4 in full (until fix, data_window provenance, cache, replay).**
   Directness 2/5, no columns; gates *reproducibility*, not *observation
   content*. Only the `n_features` component ships (mandatory-with-widening);
   the `until` clip fix (~8 LOC, RESEARCH-2 §1.3) and full provenance suite
   stay deferred, correctly sequenced after Gap 1 per RESEARCH note 5.
5. **`order_book_imbalance` via the engine recorder (Gap 3 in-repo fallback,
   RESEARCH-1 item 2).** +1 column to 56, needs a ~25-30-line engine
   aggregator, and is only correct if the engine loop is the deployment. The
   60 s engine `Depth` call keeps being made and discarded (Gap 3's "machinery
   already running" fact) — that is recorded as the future producer, not split
   into this slice.
6. **On-chain / macro / academic / new-text-news.** Re-verified by the audit
   and carried forward unchanged (AUDIT.md "Categories deliberately NOT ranked"):
   paid/API-keyed, needs an asset→pair mapping layer, weekly/daily horizon vs
   hourly observation, or no route to a per-(ticker, hour) numeric. No new
   evidence overturns them.

## 7. DEFERRED, WITH WHAT MUST HAPPEN FIRST

Same table, current status per `ca360a8` (from AUDIT.md deferred summary):
Candidate 2 (`spread`) — **now ship** (part of Gap 1 scope); Candidate 3
(`until` + window record) — after this slice, needs the fix sequenced with a
window-recording step (RESEARCH note 5); Candidate 4 (schedulers + flake) —
full version after this slice's single timer proves the pattern; Candidate 5
(retry/backoff, `transport.py`) — sibling-repo boundary, unchanged; Candidate 6
(book/trade recorder) — remains the next NEW-DATA-SOURCE after a scheduler
exists; `kraken-deep-history seed` / `market_data_store` — ops, still un-run;
`signal_max_age_hours` per-source bounds — filed schema change; A5 defaults
drift (`_FEATURE_GROUPS`, `pages=6` ×5, `_FETCH_PAGES=2`) — cheap, orthogonal,
also still open; width guard / `n_features` — **now ship** (Gap 1 scope).

## 8. GUARDRAILS HONORED

- **Keyless preference**: Gap 1 makes zero additional Kraken calls; the funding
  file comes from an already-keyless sibling CLI. The recorder (keyless) is the
  only keyless *new-source* candidate and it is deferred, not preferred.
- **Rate limits**: no new REST traffic this slice. One funding pull/hour ≈ 0.0003
  calls/s against a 15-20/s tier — negligible; the existing per-tick cost
  (2 OHLC pages + 3 JSONL parses) is untouched.
- **`spread` single-writer**: funding `bid`/`ask` → `spread` is the only owner;
  any future tape recorder must use `realized_spread_bps` (recorded in
  RESEARCH-3 §4.4, honored here).
- **Widening = guard + retrain**: `n_features` provenance and a non-tautological
  width assertion ship in the same slice; the six 49-wide artifacts are deleted
  and retrained at 55, never backtested at the wrong width.
- **Presence-gated, not required-column**: new builders read their inputs
  `if present` exactly like `_add_microstructure_features`, so a frame without
  `vwap`/`count` still computes (width probe: 49 unchanged when inputs absent).
- **One clean outcome**: Gate number 55 + the guard + the funding timer, in one
  repo, no new dependency, TGIF-compatible with the standing /tmp-scratch and
  recorded-window constraints from PLAN.md §7.

---

## 9. PHASE 4 VERDICT

**Phase 4B — implement in the existing target.** Gap 1 widening 49 → 55
(`_SIGNAL_COLUMNS` + seam derivation), `n_features` guard, minimal funding-file
activation under one `systemd.user` timer. Touched repo: `kraken-trading-bot`
only; `kraken-funding-rates` invoked, not modified. No new repo, no submodule,
no new dependency. Runner-up (microstructure recorder / NEW-DATA-SOURCE) filed
as the next project once a scheduler exists.

DECISION COMPLETE