# RESEARCH — consolidated (2026-09-30 pass)

Lead-assembled from the three per-researcher files:
`.data-audit/RESEARCH-1.md` (Candidate 1 — normalization),
`.data-audit/RESEARCH-2.md` (Candidate 2 — market-data depth),
`.data-audit/RESEARCH-3.md` (Candidates 3+4 — ops seams + fetch reliability).
Full detail, evidence, and scoring tables live in those files; this file is
the Decision-phase digest.

---

## R1 — Candidate 1: normalization.npz never shapes the observation (IMPROVE-EXISTING)

Researcher 1 reproduced the red test and scored 5 approaches. The machinery is
**already complete in-repo** (`NormalizationStats.fit/transform/fit_transform`
+ npz save/load + per-ticker keying + std-floor); the gap is *wiring and
ordering*, not a library. Zero new dependencies.

Three concrete defects (severity order):
1. Transform never applied to the observation — policy trains on
   `_raw_feature_array()` = compute + ffill + fillna(0) (`environment.py:445-448`);
   the npz is saved (`train.py:238`) and loaded (`backtest.py:161`,
   `paper_trade.py:188`) then ignored. Dollar-scale columns beside unit-scale
   columns in one Box.
2. Fit-frame ≠ observation-frame (the red test): `fit()` stores stats of the
   **raw NaN-warmup** compute frame; the observation is the **ffilled** matrix —
   so `z_*` ≠ affine(obs) and the test fails.
3. Look-ahead: `prepare_episode` fits on the full frame then slices the episode
   (`data.py:547,557`) — held-out tail leaks into stats.

**Top recommendation: Approach A (full wiring, ~20-30 lines, ~1-1.5 d, zero
new deps).** Reference: SB3 `VecNormalize` (already a dependency; pattern-only,
don't wrap in VecEnv — the export CSV must mirror it). Make fit/transform
operate on the **ffilled** observation frame, have `prepare_episode`
**slice-first-then-fit**, and apply the ticker's fitted stats on
`environment._observe`/`_raw_feature_array` AND `paper_trade._build_observation`.
`models/` is empty → no retrain burden. Red test passes as-written and becomes a
true contract. Approach B (fit-frame + ordering only, ~0.5 d) is the CI-green
clamp; leaves the conditioning win deferred.

**Do NOT delete the npz stack** — verified blast radius: `backtest.py:155-161`
and `paper_trade.py:187-188` use the npz as the *whether-to-build-the-49-feature
-pipeline* guard (deleting → SB3 obs-space mismatch vs builtin 8-feature obs);
`cli.py:428`/`registry.py:82` gate `paper-trade` on `is_trained()` requiring the
npz; and the red test is the only regression guard for this bug class.

Heavy-tail hardening (RobustScaler/Quantile/Power) is **deferred follow-up**:
nonlinear variants break the npz `means`/`stds` format and the red test's linear
`z = (x−mean)/std` contract.

## R2 — Candidate 2: market-data depth + clean splits

Researcher 2 verified the 720-bar cap from Kraken's own OpenAPI ("Older data
cannot be retrieved, regardless of the value of `since`"), the Binance archive
live, and the store seam line-by-line.

- **The 720-cap is a fact, not folklore:** Kraken = forward-poll venue, never
  backfill. Empire-re-proven at `--pages 21` → 721 bars.
- **The store read path already plumbs `since`/`until`** (`data.py:459`,
  `store.py:168` month-partition pruning) and is unit-tested — but **none of the
  four production call sites** (train.py:185, backtest.py:138, paper_trade.py:289,
  export.py:159) pass them, and config carries no such keys. Clean splits are a
  config/CLI + one-function change, **not** a store change.
- **Top source: B1 Binance public data archive** (`data.binance.vision`) — the
  seeder sibling `kraken-deep-history` already reduces it to store schema.
  Probed live: ETH/BTC 1m back to 2017-08, SOL 2020-08, XRP 2018-08, DOGE
  2019-07; all intervals, keyless, MIT helper. ~9 years / ~84k 1h bars for
  ETHUSD. **The blocker is not sourcing — the store root doesn't exist and
  `kraken-deep-history` is not a flake input / not on PYTHONPATH**, so seeds
  silently fall back to CSVs the bot's parquet-glob can't read (0 bars).
- **Real continuity facts:** 14 genuinely-absent 1h bars in 2021/2023 across 7
  runs, and **two zero-volume bars** that crash PPO via `volume.pct_change()` →
  inf → NaN logits (recorded upstream fix: treat non-finite like NaN in both
  observation constructors). `verify` exists in both siblings (read-only gate,
  same JSON shape).
- **Interval:** 1 bar = 1 PPO step; 10k timesteps ≈ 416 d of 1h vs 7 d of 1m.
  Seed 1h for train/eval; 1m/5m only for paper/microstructure as a sub-store.
- **Flake wiring (2 small edits):** add `git+https://github.com/Cairnstew/
  kraken-deep-history` input + shellHook PYTHONPATH prepend; the bot dev shell
  already has pyarrow+requests+market_data. Keep `market_data_store: null`
  default; enable per-run (deep-history.example.yaml precedent).
- Coinbase (300/call, interval gaps) and CryptoCompare (same clamp, keyed)
  ruled out. `kurosearch` (Rule34 app) / `researcher-python` (academic
  aggregator) confirmed NOT market-data-adjacent.

## R3 — Candidates 3+4: ops seams + fetch reliability

- **Scheduling:** only `kraken-market-data/nix/module.nix` ships a
  systemd module+timer (oneshot, `DynamicUser`, `StateDirectory`,
  `OnCalendar=*-*-* *:*:30`, `Persistent=true`) — the exact template for the 3
  signal siblings, which ship NO timer. Bot `flake.nix:4-8` lists only
  kraken-python + kraken-market-data as inputs so the 3 CLIs are unreachable in
  `nix develop`. Caveat: ticker-news-signals needs its gnews/vader overlay;
  funding+social are plain `buildPythonPackage`.
- **Staleness:** `merge_extra_features` (`data.py:168-171`) ffill+zero-fills
  silently — a 5-day-old funding_rate is byte-identical to a fresh one.
  `novelty_flag` computed only at pull time; F&G missing-day encoded as 0
  (= extreme fear) is Candidate 6. Fix: a `signal_age_hours` column — either
  emitter-side `fetched_at` (best provenance) or merge-side asof (keep the
  ffilled row timestamp; pandas `merge_asof` is the reference). ~10 lines.
- **Forward `funding_rate_prediction`: real win, zero cost** — current
  `funding_rate` is stale-by-design (settles ~8-hourly); the exchange's own
  predicted-next rate is already in every JSONL record. One allow-list entry ×2
  (the `_SIGNAL_COLUMNS` duplication again). **`index_price`: NOT recommended**
  (dollar-scale, collinear).
- **Retry/throttle/cache (Candidate 4):** `kraken-python/transport.py` is the
  family's one transport without retry/backoff (min_interval 0.0 default,
  RequestException/RateLimitError raised immediately, `transport.py:108,210-219,
  249-250`). **The fix is already proven in-family** — both
  `kraken-funding-rates/client.py:70-95` (exp backoff on 429/5xx/RequestException)
  and `kraken-market-data/client.py:115-142` (limiter+backoff) ship it. Approach:
  (1) hand-rolled exp-backoff in `_request` (~25 lines, no new dep — tenacity/
  backoff are cites but unnecessary), (2) flip `min_interval` default to ~0.05,
  (3) TTL cache (cachetools or 5-line monotonic dict, NOT lru_cache) for the
  100-bar window + the `data.py:401-406` append/tail TODO for the paper trader
  (biggest per-tick waste: 2 pages + full recompute + 3× JSONL reparse per tick).
- **Recommendation ranking (cost-to-protect):** 1) transport retry/backoff +
  min_interval default; 2) forward `funding_rate_prediction`; 3)
  `signal_age_hours` staleness column; 4) flake inputs + market-data-style
  systemd timers for the 3 seams; 5) TTL bar cache + paper append/tail.

---

## Synthesis for the Decision phase

- **Candidate 1 is the single highest-directness observation-impacting item** and
  is pure-code (zero sourcing, zero new deps). It is also the *precondition* for
  any new-source column (Candidates 3/5) to be usable at comparable scale — the
  Pipeline currently trains on raw heteroscaled obs.
- **Candidate 2** unlocks depth the audit identifies as the stakeholder-level
  gap; the machinery exists but has never populated a bot-visible store; wiring
  is 2 flake edits + a seed run. Additive to C1 (clean splits need the same
  slice-first-then-fit fix).
- **Candidates 3+4** are cheap, protection-oriented (retry everywhere,
  staleness honesty, the funding_rate_prediction forwarding).
- No researcher recommends skipping C1 for a shiny new source. The red test in
  the tree is the live pressure to fix C1 first.

RESEARCH COMPLETE