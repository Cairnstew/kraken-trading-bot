# Research — data-pipeline audit 2026-10-01 (assembled)

Lead-assembled index over the three per-researcher files. Each researcher was
read-only and wrote its own file to avoid write races. Evidence anchors are in
the per-researcher files and in the authoritative on-disk `.data-audit/AUDIT.md`
(revised final version; a draft with different gap numbering was superseded —
the ranked *substance* is unchanged between the two).

| Researcher | Gap (authoritative AUDIT) | File |
|---|---|---|
| 1 | Gap 1 — produced-but-unused data (vwap/count, order_book sizes, funding bid/ask, funding_rate_prediction/vol24h) | `RESEARCH-1.md` |
| 2 | Gap 4 / Gap 2 — data quality: dropped `until`, provenance, pages=6, no scheduler, paper-trade refetch | `RESEARCH-2.md` |
| 3 | Gap 3 — market microstructure: keyless-but-unused `recent_trades`/`spread`; producerless `order_book_imbalance` | `RESEARCH-3.md` |

---

## RESEARCH-1 (Gap 1 — produced-but-unused data)

VERIFIED: `vwap`/`count` parsed at `data.py:649,651` → `_OHLCV_COLUMNS`
(`data.py:58`) → stored + exported → read by **zero** feature builders.
`order_book` fetched every 60 s (`engine.py:72`) → `tick()` → never read by
`sma.py`; `features.py:414-418` already defines `order_book_imbalance` needing
`{bid_vol, ask_vol}` that **no producer anywhere emits**. Funding `bid`/`ask`
emitted (`kraken_funding_rates/models.py:63,64`) → dropped at allow-list
(`data.py:420-424`); `features.py:410-413` `spread` builder is ready +
test-covered. `funding_rate_prediction`/`vol24h` emitted (`models.py:58,65`) →
same allow-list drop; prediction is the ~8h-forward number vs settled
`funding_rate`; `vol24h` is futures 24h turnover, a different quantity from bar
`volume`.

**Measured current base width: 49** (researcher ran the real pipeline in `nix
develop`: price=10, technical=33, volume=6, micro=0, signals=0). All 7 additions
→ 56 with clean accounting; naive whitelisting of 4 raw inputs gives 59.

Width-guard consequence verified: `paper_trade.py:327-340` is tautological for
the stale-model threat — `NormalizationStats.normalize` (`features.py:138-144`)
silently DROPS columns not in `feature_names`, so the 6 existing 49-wide models
load and run column-less after widening (safe but invisible). `backtest.py` has
NO guard. Recommendation: ship widening with a retrain + add `n_features` to
`registry.py:60-76`.

Recommended minimal set: items 1+3+4 (+6 → 55), zero new repos/calls. Item 2
(`order_book` sizes → `order_book_imbalance`, +1 → 56) only if the engine loop
is the intended deployment (it is the only thing fetching a book today). Full
implementation shapes + verification hooks in `RESEARCH-1.md` §5–6.

## RESEARCH-2 (Gap 4 + Gap 2 — data quality, reliability)

**until-gap — two layers:** `fetch_ohlc_dataframe` (`data.py:706-717`) has NO
`until` param; `read_ohlc_dataframe`'s live branch (`data.py:875-887`) doesn't
forward it. Store branch already correct (store.read filters). Kraken OHLC only
takes `pair/interval/since` (`kraken-python client.py:99-114`) → `until` MUST be
clipped client-side: `_page_candles` stops when `batch[0].time >= until`
(ascending candles), then trim df. Minimal fix = 3 sites ~8 LOC + 1 test. Config
surface: `data_window: {since, until}` block + `window_bounds(cfg)`. Semantics:
`until` valid only WITH `since` on live path; store path honors each
independently. Zero callers pass `since=`/`until=` (grep-confirmed).

**Provenance:** `registry.py:61-78` `config_summary` has 6 keys, no
`n_features`, no window, no hash. Recommended added fields:
`data_window.{actual_start,actual_end,n_bars,interval_minutes,store,source,data_hash}`
+ `n_features` (`env.observation_space.shape[0]`, not pipeline.n_features()) +
`feature_fingerprint` (incl. the `_SIGNAL_COLUMNS` constant). `cli.py models`
surfaces via config_summary (`--json` picks it up free).

**pages=6:** 5 code sites (`data.py:709,794`; `train.py:133`; `backtest.py:91`;
`export.py:128`) + 3 argparse defaults (`cli.py:156,231,290`). Centralize as
`DEFAULT_OHLC_PAGES=6` in `rl/data.py`; `_FETCH_PAGES=2` (paper_trade.py:56) is
the deliberate exception — keep separate.

**Scheduler — current state:** `module.nix:220` = EnvironmentFile writer only,
zero timers; `flake.nix` has 2 signal inputs; kraken-market-data is the only
sibling timer but its ExecStart is broken (invalid choice: 'market');
news/funding/social have no usable nix timer. Options ranked:
(A) `systemd.user` timer+service pairs in THIS repo (cheapest, works with
`~/Projects` roots) → (B) add 3 siblings as `git+https` flake inputs
(buildability fix; private inputs need `git+https`, not `github:`) → (C)
flake-pinned `systemd.timers` in `nix/module.nix` (most robust, needs B) → (D)
fix+use sibling modules upstream (eventual home). Cadence:
news/social hourly, funding ~3×/day with `--append`, market-data hourly.

**Paper gate:** verified per-tick refetch (`paper_trade.py:280-382`; 2 pages + 3
JSONL parses + full compute + predict per step; loop sleeps 60s). Gate =
`_last_bar_ts` compare: recompute+predict+execute ONLY when `df.index[-1]`
advances (provably no-op-safe mid-bar). JSONL-parse cache keyed
`(path,size,mtime_ns)` is the complementary cost cut. A true stop-polling gate
needs websockets (out of scope).

**Recommended minimal set (7 items, ranked):** until fix → config+callers →
provenance+models surfacing → DEFAULT_PAGES → scheduler A then B → paper gate.
Explicitly excluded: retry/backoff (sibling boundary), ownership migration,
upstream modules, microstructure recorder.

## RESEARCH-3 (Gap 3 — market microstructure)

VERIFIED: `recent_trades` → `(list[Trade], last)` (`manager.py:192`); Trade row
`[price,volume,time,side,order_type,misc]` — trade_id is DROPPED by wrapper,
time truncated to int. `spread` → `(list[SpreadPoint], last)` (`manager.py:200`);
`[time,bid,ask]` strings, NO volume component, ~200-sample rolling window
("does not contain all historical spreads"). Depth count ≤500; engine already
fetches count=10 every 60s and discards sizes.

Cursors: both wrappers return `(rows, last)` exactly like `_page_candles`
(`data.py:691-703`) — pattern transfers 1:1. KEY nuance: trades `since` is an
OPAQUE string id, not a timestamp → resume-from-last only, persisted in a
`_meta.json` sidecar (the kraken-market-data pattern). Spread `since`/`last` is
an int.

Rate limits: official "Spot REST has a call counter (max 15-20 depending on
tier)". Recorder at 60s × 3 pairs × 2 endpoints ≈ 6 calls/min (~100× under).
Completeness is a CADENCE question not budget: Trades caps 1000 rows, Spread
~200 samples → a 5-min+ cron silently drops the tape; **15-60s poll works**.

**RECOMMENDED SHAPE — new keyless SIBLING recorder (option ii):** poll
trades+spread at own cadence, emit per-(ticker,hour): `taker_buy_vol`,
`taker_sell_vol`, `taker_imbalance`, `trade_count`, `mean_trade_size`, `vwap`,
`last_price`, `vwap_pressure`, `realized_spread_bps` (+optional
`bid_vol`/`ask_vol` if it also polls depth → `order_book_imbalance`). Why
sibling over in-repo: (1) tape only exists if someone listens — in-repo records
books only while the engine runs AND emits no tape scalars; (2) keeps
acquisition off the live-trading path; (3) kraken-market-data already owns the
since-cursor + NixOS-module scaffolding. In-repo (option i, reuse engine
order_book → spread/bid_vol/ask_vol, ~30 lines) is the correct fallback if ONLY
order_book_imbalance is wanted with zero new processes.

**COLLISION WARNING for the architect:** Gap 1's activation of funding
`bid`/`ask` → `spread` and a recorder must NOT both own the column name
`spread` — `merge_extra_features` applies files in sequence
(`data.py:779-786`), last-write-wins is undocumented. Recorder should use
`realized_spread_bps`; keep `spread` single-writer.

Scores: feed-directness 4/5, difficulty 3/5. ccxt (option iii) rejected as
producer — the seam is Kraken-spot-aligned, per-exchange `since` semantics, 3
repos of dep for a validation tool only.

---

## Cross-file notes for the decision

1. **The dominant outcome evidence is IMPROVE-EXISTING.** Gap 1 (dead columns)
   scores 5/5 directness / 1/5 difficulty, is fully in-repo, needs zero new
   API/dependency/repo, and is the natural continuation of the pass-2 sequence.
   Its honest caveat: the 6 columns produce 0 until a `*_features_file` is
   configured (Gap 5) — so a coherent slice is **Gap 1 columns + Gap 5
   activation (at least a funding file via the sibling CLI under a systemd.user
   timer, i.e. one slice of Gap 2)**.
2. **NEW-DATA-SOURCE remains legitimate** as the microstructure recorder (Gap
   3 / RESEARCH-3): keyless, already-wrapped endpoints, per-hour named scalars
   fitting `_SIGNAL_COLUMNS` directly; cost = a new sibling repo + a recorder
   process. It competes with Gap-1's zero-friction in-repo slice.
3. **Any widening must ship with a guard fix** (`n_features` provenance +
   non-tautological width assertion), otherwise the 6 on-disk 49-wide models
   silently run column-less — safe but invisible.
4. **Do not double-own `spread`** (single-writer rule).
5. **Until fix is cheap and gates reproducibility** but changes no column; it
   belongs with Gap-1 only if the slice keeps a window-recording step.