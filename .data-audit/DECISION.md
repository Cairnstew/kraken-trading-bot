# DECISION — Phase 8: one (gap, library) pair for this pass

Author: Architect, audit-pipeline team. Read-only phase — this document decides; it does
not scaffold. Fresh Phase-8 decision, superseding the Phase-3 Gap-1 decision (store) that
was built and validated in `kraken-market-data`; this decision is taken **after** the store
and funding seams landed, against the fresh AUDIT.md and the three live-verified research
surveys (social, microstructure, on-chain; 2026-09-28).

## 1. THE DECISION

- **Chosen gap: CANDIDATE 1 — Social / search-trend** (original Gap 3, still open) — the
  audit's own "best new modality" and the only remaining gap whose source set is both
  keyless **and** immediately backfillable for train/backtest.
- **Libraries: none new — plain HTTP via the house transport/client pattern** exactly as
  the prior sibling passes use (`ticker-news-signals`, `kraken-funding-rates`): an
  `requests`-style thin client in `transport/client` modules, a manager facade, typed
  dataclasses (raw `int`/`float`/`str`, no lossy floats), `export.py` registry, offline
  tests + a live-verify script. No ccxt, no pytrends, no LunarCrush SDK. Both endpoints
  are plain REST JSON (StockTwits v2, alternative.me F&G) — research verified no wrapper
  is needed or maintained (blockchair-python-style stale wrappers rejected by precedent).
- **Project name: `kraken-social-signals`.**
- **Path: `../kraken-social-signals/` — a sibling git repo**, not nested in this repo and
  not a submodule (same placement as `ticker-news-signals`, `kraken-market-data`,
  `kraken-funding-rates`).

Name rationale: it is Kraken/crypto-specific — it produces the per-(ticker, hour) crypto
sentiment axes for *this* bot's RL feature vector (StockTwits retail mention velocity on
`BTC.X`/`ETH.X` plus the market-wide Fear & Greed index), so it belongs in the `kraken-*`
family, consistent with the sibling signal projects. A source-oriented name
(`stocktwits-poller`) would understate that it is one of the bot's own exogenous signal
seams; `kraken-social-signals` makes the family obvious and matches the house-style map in
RESEARCH.md (Researcher-social).

## 2. WHY THIS PAIR, AGAINST THE PIPELINE'S REAL SHAPE

The landing point is **proven twice over** — this is the decisive fact. The bot's RL
observation path (`configs/default.yaml` → `rl/data.py` → `rl/features.py` →
`models/{TICKER_ID}/{model_name}/`) already has the exact seam this source needs:

- **Config keys already documented for sibling JSONL seams:** `extra_features_file: null`
  (`default.yaml:57`) for news and `funding_features_file: null` (`default.yaml:65`) for
  funding. `feature_groups` already contains `"signals"` (`default.yaml:48`). A social
  seam is a *third* key in that proven row: `social_features_file: null`.
- **The seam itself (`merge_extra_features`, `data.py:80-177`) is byte-identical in
  behavior for any per-(ticker, hour) scalar file:** hour-floor left-join + ffill +
  zero-fill. It is already invoked twice per load (`fetch_ohlc_dataframe` `data.py:319-320`,
  `read_ohlc_dataframe` `data.py:430-431`); a third invocation for the social file is the
  same 2-line pattern the funding pass already established.
- **`_add_signals_features` (`features.py:388-401`) forwards ANY numeric column already in
  `_SIGNAL_COLUMNS`.** The only code change required is the documented 2-file widening
  (`data.py:49`, `features.py:32`) by `stt_mention_count`, `stt_tilt`, `fng_index`. The
  audit's seam caveat (`AUDIT.md:112-117`) is acknowledged: the widenings must stay in
  sync, and models must be retrained for the columns to enter the observation (see §5).
- **Models layout land:** `FeaturePipeline.compute()` → `signals` group → the observation
  vector; per-ticker stats are persisted to `models/{TICKER_ID}/{model_name}/normalization.npz`
  and `config.yaml` records the merged `default.yaml` (via `build_train_config`), so a
  retrained model carries the widened width and the new columns' stats. This is exactly
  where a feature-engineering step reads the feature per ticker — same artifact tree the
  news/funding columns already land in.
- **Backfill is the differentiator vs the two other research-tested candidates.**
  `train_ticker`/`backtest` knobs (pages, since/until) bound the OHLC frame at ~720 bars
  (~30 d) without the store — and even through the now-existing store, the *social* pair
  can be deep on day one: alternative.me F&G `limit=0` returns full 2018→ history in one
  keyless call (`fng_index`), and StockTwits cursor-walk reaches the 60–180 d train window
  (`stt_mention_count`, `stt_tilt`). No forward-only wait: train/backtest get a labeled
  history immediately.

Sourcing guardrails: both sources keyless, no geo-block, ToS-clean (F&G is an offered
public API; StockTwits is user-generated posts with attribution requested). StockTwits'
one caveat — Cloudflare bot-challenge on scripted HTTP — is a client hardening (browser UA,
retry/backoff, non-fatal on challenge), not a blocker; it is folded into the client spec.

## 3. INTEGRATION SKETCH (how the bot consumes it)

**Output contract — the JSONL file `kraken-social-signals` writes:** one record per
(ticker, hour): `{ticker: "ETH/USD", timestamp: <ISO-UTC hour>, stt_mention_count: <int>,
stt_tilt: <float>, fng_index: <int>}` — same shape family as the news and funding JSONLs,
so `merge_extra_features` consumes it with zero pipeline redesign. **Config key:**
`social_features_file` added to `configs/default.yaml` (null = off), the third sibling
JSONL key in the same documented pattern; the bot's `fetch_ohlc_dataframe` /
`read_ohlc_dataframe` gain the matching parameter and the third `merge_extra_features`
call. **Refresh cadence:** hourly `cli.py pull --ticker ETH/USD --output
signals/eth_usd_social.jsonl` (mirrors the news pass; F&G is daily and the merge's ffill
hands the sub-day gap exactly as it does for funding). NixOS module optional — both
sources are cheap enough for the manual/cron hourly pull; a systemd timer is a possible
add-on later, not a v1 requirement. **How the bot reads it per ticker:** `signals` group →
`_add_signals_features` → raw observation columns with per-ticker normalization stats in
`models/{TICKER_ID}/{model_name}/` exactly like the two completed seams.

## 4. RUNNER-UPS AND WHY THEY LOST

1. **Gap 4 / microstructure recorder (Kraken REST poller via house lib, zero new deps).**
   The land-shape is arguably the cleanest — it activates the *dormant* `microstructure`
   group (`features.py:373-386`) by writing `bid/ask/bid_vol/ask_vol` onto the frame with
   **no `_SIGNAL_COLUMNS` widening at all**, and the research overturns the old blocker
   (store now exists). But it is **forward-only** — no keyless depth history anywhere —
   so train/backtest get nothing until weeks of runtime accumulate, and the operational
   cost is medium-high (poller + storage + alignment + store must be enabled). It fails
   the "train/backtest today" bar this pass targets. It is the natural *next* plumbing
   pass once `market_data_store` is actually enabled on a host.
2. **Gap 5 / on-chain (Blockchair v2 + Coin Metrics Community + DefiLlama + Whale Alert
   archive).** Deep keyless backfill is real (2019 blocks, 2022 txs — overturns the
   audit's "backfill-limited" fear), but Blockchair v2 is a **maintenance-mode API** (last
   major update 2022-11-07, successor 3xpl) — the wrong reliability profile for the bot's
   first non-venue exogenous heartbeat — and its strongest legs are chain-level/daily
   (Coin Metrics 1h is paid-403; DefiLlama daily), cutting per-ticker hourly directness.
   The audit's "park until the store has depth" still stands.
3. **Gap 2 — funding rates** — already built (`kraken-funding-rates`), acknowledged
   (near-constant 8h cadence, not a bot flake input; not this pass's problem).
4. **Candidate 2 — normalization.npz wiring** — pure code fix, zero sourcing, highest
   directness of all; but this pass's mandate is a (gap, library) *data-source* pair, and
   making z-scores real is a code change inside this repo, not a sibling project. Recorded
   as companion hardening (§5). Same for **Candidate 3 scheduling** (ops, not a source).

## 5. EXPLICIT NOTE ON WHAT THIS PASS DOES NOT BUILD

- **No microstructure recorder / WS consumer / store extension.** No on-chain project.
  No macro calendar. Both stay parked (runner-ups above).
- **No normalization wiring fix** (Candidate 2) — this pass does not touch
  `environment.py`/`paper_trade.py` to apply `transform()`. It pairs naturally with the
  new heteroscaled columns (raw mention counts, 0–100 F&G) and can be the next hardening,
  but is explicitly **not** this pass's deliverable.
- **No scheduler for exogenous pulls** (Candidate 3) — timers for news/funding/social
  remain manual/cron as documented; the bot's `nix/module.nix` stays credentials-only.
- **No `_SIGNAL_COLUMNS`-from-config refactor.** The audit flags reading that tuple from
  config so future sources stop needing the 2-file edit; this pass uses the *minimal
  proven widening* to land the feature, and records the config-read + a
  sync-check between the two tuples as follow-up hardening. Also **no model retraining**:
  on-disk `models/ETH_USD` / `models/XRP_USD` are at 49 features (pre-seam); retraining
  after the social seam is enabled is a downstream step, not a scoped deliverable.
- **No paid or ToS-grey legs:** no LunarCrush social tier (~$90/mo), no X API, no Reddit
  scrape, no pytrends/Google Trends scrape, no BlueSky phase-2 leg. The pass builds the
  two keyless, ToS-clean columns (StockTwits velocity/tilt + Fear & Greed) and nothing else.

DECISION COMPLETE