# RESEARCH — synthesis of RESEARCH-1/2/3 for the architect

**Assembled by the lead from the three per-researcher files.** Full evidence, per-candidate
records, and every "could not determine" live-check is in those files — read them before
committing to anything, but the decision can be made from this page.

| File | Gap | Lines | Ranked recommendation |
|---|---|---|---|
| `.data-audit/RESEARCH-1.md` | **G1** signal seam unreadable path | 565 | One shared path resolver + loud-on-broken + a non-vacuous test |
| `.data-audit/RESEARCH-2.md` | **G2** microstructure has no RL path | 540 | Two near-free Kraken-spot features FIRST; then a Binance-seeded sibling |
| `.data-audit/RESEARCH-3.md` | **G3+G4** can anything be concluded? | 344 | Kraken's own archive for G3; a computed detection floor for G4 |

---

## 0. READ THIS FIRST — the three researchers corrected AUDIT.md six times

The audit is the starting hypothesis, not the ground truth. In order of consequence:

1. **G1's headline consequence is wrong in a way that makes it worse, not better.** AUDIT says 12
   columns are "permanently zero" and `signal_observed == 0.0` on every bar. RESEARCH-1 measured
   it: the columns are **absent, not zero-filled**, and `signal_observed` **is not in the
   observation at all**. Width as-shipped (52) is **identical** to width-with-the-broken-path-
   configured (52); 59 only with a real funding record. So there is **no width signal at all** —
   `check_feature_width` cannot detect this by construction, and the diagnostic column the audit
   proposed reading is itself missing.
2. **"Lazily create `signals/`" is not the defect — rejected on evidence.** Both producers already
   `mkdir(parents=True)` (`kraken-funding-rates/export.py:140`,
   `kraken-social-signals/export.py:140,148`) and `.gitignore:65` ignores `signals/`, so its
   absence is *correct* for a clean checkout. The real ops gap is only the uninstalled timer
   (`systemctl is-enabled` → `not-found`, confirmed live).
3. **G2's stated contrast is half wrong.** `/public/Spread` is *also* effectively forward-only —
   its own docs say `since` "does not contain all historical spreads", it returns ~200 samples,
   and `SpreadPoint` carries **no volume** so it can never produce an imbalance. The real
   asymmetry is **Depth vs Trades**, not Depth vs Spread.
4. **G3's venue caveat is free to remove.** AUDIT warns Binance-archive-under-Kraken-pair-ids is
   a discontinuity. Kraken publishes its **own keyless OHLCVT archive** (RESEARCH-3 verified by
   download and parse), which removes the venue seam *and* the DOGE hole at once.
5. **G4's intuition is inverted.** AUDIT frames power as a function of depth. Measured
   σ(L) ∝ √L (lag-1 autocorr of window returns **+0.993…+0.997**): a longer window is a *noisier*
   measurement. One 456-bar window ≈ **3 effective independent draws**. Depth buys σ measurement,
   not σ reduction — so **G3 is what makes G4 fixable, and depth currently contributes zero
   variance reduction.**
6. **The width numbers are disputed three ways and nobody reproduced them.** Prior RUN LOG says
   49/52/60; RESEARCH-1 *measured* 52/52/59; RESEARCH-2 *computed* 51/54/67 and could not run a
   probe at all (its venv fails to import numpy — `libz.so.1` missing). **Do not gate on any of
   these numbers until they are re-derived from the real pipeline.**

---

## 1. G1 — the shipped signal seam that cannot read its own configured file

`data.py:363` resolves the config-supplied signal path with `Path(x)` and **no `expanduser()`**,
while `configs/default.yaml:79` ships `~/Projects/kraken-market-data/signals/eth_usd_funding.jsonl`.
The same real file yields `[]` columns via the shipped tilde spelling and
`['funding_rate_prediction','vol24h','bid','ask','signal_observed']` via the expanded one —
wrong from **every** CWD. `backtest.py:219` already calls `expanduser()`, so the correct pattern
lives in this repo.

**Directness 5/5, keyless, trivial.** Four findings beyond the audit:

- **A second instance of the same bug:** `data.py:1054` `MarketDataStore(Path(...))` omits
  `expanduser()` too, and `deep-history.example.yaml:81` ships a `~` path — so the same one-line
  defect **silently blocks G3's store fix**. Five sites total (incl. `train.py:82`,
  `export.py:250,263`).
- **A latent cross-ticker contamination bug:** the funding record has **no `ticker` field**
  (`kraken-funding-rates/…/models.py:50-65`; keys are `symbol`/`spot_pair`/…), while news and
  social both emit `ticker`. So `signal_require_ticker: true` is **inert for the only configured
  channel** — and `default.yaml:78` actively invites pointing a SOL model at the ETH funding
  file, which would merge ETH funding onto SOL bars silently.
- **The test that should have caught it is vacuous:**
  `tests/test_rl_signal_config_wiring.py:711-734`, literally named *"config points at a REAL
  funding file"*, only asserts the string is non-empty and ends `.jsonl`. Replayed verbatim it
  **passes for `/definitely/not/here/nope.jsonl`**. That is why it shipped.
- **A coupled-test trap:** `tests/test_model_matrix.py:1416-1418` pins **the bug's own warning
  text**, so a fix that changes the message turns the suite red. Move them in lockstep.

**RESEARCH-1's ranked plan** (all IMPROVE-EXISTING, no new repo):
`R1` one `_resolve_config_path()` helper applied at all five sites (~4 lines) ·
`R2` at `merge_extra_features` (`data.py:360-368`, `382-384`): `null`/empty ⇒ silently off (unchanged,
first-run UX preserved); **non-null but unresolvable ⇒ RAISE**, naming key + raw + expanded value
+ producer command (`backtest.py:218-224` is the in-repo precedent); resolves-but-0-records ⇒ raise,
promote `DEBUG`→`WARNING`; no-ticker-overlap ⇒ keep today's WARNING ·
`R3` replace the vacuous test with a non-vacuous one (fake `HOME` + `~` path, assert the seam
*consumed* the file) · `R4` correct the 49→55 records in `features.py:270`, `default.yaml:104-107`,
and delete the false `see nix/module.nix` cross-reference (`default.yaml:73-74` — that NixOS
module contains **no** timer and no `systemd.user.*`; `systemd.user.*` will not evaluate there) ·
`R5` ops: `just funding-timer` (`justfile:140`), add news+social units · `R6` (sibling repo) add
`ticker` to the funding record.

Sequencing note from RESEARCH-1: **R1+R2 alone** convert a silent 52-wide pipeline into a loud
failure — that is the whole of G1's risk. R3 stops it recurring. R5 makes it produce data.

---

## 2. G2 — microstructure: fetched every 60s, discarded, and (mostly) unbackfillable

The 60s loop calls `order_book(count=10)` → `data["order_book"]`, documented as a `tick()` input at
`base.py:92`, but `sma.py` reads only candles + ticker. `order_book_imbalance`
(`features.py:581-585`) is the only reader of `bid_vol`/`ask_vol` and has **zero producers**
repo-wide.

**The asymmetry verdict (the question that gated everything):**
- `/0/public/Depth` — params are **only** `pair`/`assetVersion`/`count(1-500)`. **No `since`, no
  `last` cursor.** Order-book history is **structurally unobtainable from Kraken**.
- `/0/public/Trades` — `since` + `count(1000)` + a `last` cursor ⇒ **genuinely backfillable**
  (retention depth unverified — needs a live check).
- `/0/public/OHLC` — official docs: "older data cannot be retrieved, regardless of the value of
  `since`". This is the ~720-bar ceiling, now confirmed from Kraken rather than inferred.

**But the asymmetry is not global:** Binance Vision publishes free historical L2 —
`data/futures/um/daily/bookDepth/` and `bookTicker/`, **USD-M futures only, spot has neither**.
ETHUSDT `bookDepth` verified 2023-01-01 → 2026-09-30, keyless, no auth, ~604 KB/day, CSV
`timestamp,percentage,depth,notional`, 2880 snapshots/day, 12 cumulative bands. That would give
`order_book_imbalance` its first history-bearing producer (120 obs per 1h bar).

**A forward-only tape is an instrument, not a training feature** (RESEARCH-2's sharpest point):
bars are 60min, the repo's smallest matrix window is 456 bars = **19 days of uninterrupted uptime**
from zero, and `models/` has never held a model. Worse, `signal_max_age_hours: 12` + the bounded
carry at `data.py:449-463` means **one 13-hour outage silently zero-fills the whole column** and
sets `signal_observed=0.0` — reproducing G1 in a second group.

**RESEARCH-2's ranked plan:**
`R1` **ship `vwap_close_gap_zscore_20` + `count_per_range` FIRST** — two rolling expressions over
`vwap`/`count`, already parsed at `data.py:717,719` and already derived at `data.py:627-688`.
Keyless, **venue-correct (Kraken spot)**, fully historical, no new seam. *"This is the whole of the
achievable Kraken-spot-native microstructure story, and it is nearly free."* ·
`R2` a `kraken-microstructure` sibling: seed from Binance Vision, record forward over the
**already-implemented** Kraken WS `book` channel (`websocket.py:43,138-166`, depth default 10 ==
`engine.py:72`'s count=10, public/no-token, CRC32+sequence — **zero WS references exist in
kraken-trading-bot**), emit one JSONL line per `(ticker, bar_ts)` onto the existing merge seam;
**exclude from `feature_groups` until seeded — never silently zero-fill** ·
`R3` fix `Trade.from_public_row` (`models.py:374-385`) first — it **drops `trade_id`** and coerces
`time` with `int()`, so any pager on `recent_trades` silently loses sub-second-ordered prints ·
`R4` Kraken `Trades` backfill (the only venue-correct + backfillable route; ~35-50 min/pair/6wk) ·
`R5` `/public/Spread` **do not use** · `R6` WS `book` alone is not a solution · `R7` skip CCXT.

**Explicitly rejected — the failure mode this whole gap is about:** do *not* snapshot the book once
and `forward_fill` it. `merge_extra_features` already ffill's every other signal with a 12h bound,
so that mistake is one line away, and **`check_feature_width` cannot detect it** — width stays
stable and correct; it would surface only as a suspiciously smooth `obi` series.

**Two discontinuities the architect must DECIDE, not discover:** (a) the archive is USD-M
**perpetual futures**, not Kraken spot — same class of seam G3 flags, and it would be fitted across
two venues; (b) WS `depth=10` (tick-based) vs `bookDepth` ±0.2% (band-based) are different
constructions, so seeder and recorder outputs **will not match at the boundary**.

---

## 3. G3 + G4 — can anything actually be concluded from this pipeline?

**G3 (thin history).** `market_data_store: null` (`default.yaml:142`) makes the deep-history
branch dead code (`data.py:949-961` is a byte-identical pass-through); the store dir does not
exist. Window bounds are a **CLIP not a push-down** (`data_window.py:212-237`; the harness warns at
`model_matrix.py:1374-1397`). Arithmetic: a pinned 456-bar window at `eval_split: 0.7` leaves ~137
OOS bars; the harness's recorded run was 672 → 202 replayable → **178 replayed**.

**G4 (power is a literal).** `MIN_REPLICATES_FOR_A_CLAIM = 3` (`model_matrix.py:230`) is static.
`summarize` computes q1/q3 and `_print_group_table` prints `median [q1,q3]`, but **no code anywhere
compares two groups for overlap** — overlapping and cleanly-separated marginals render identically.

**RESEARCH-3's verdict on G3:** swap `kraken-deep-history`'s source from Binance archives to
**Kraken's own keyless OHLCVT archive** (`support.kraken.com/hc/en-us/articles/360047124832`,
`Kraken_OHLCVT_2026Q2.zip` = 537,866,903 B, Full = 10.5 GB, sha256 `fc81b54c…8eaa4`). RESEARCH-3
**downloaded and parsed it**: `MANIFEST.json` = `{pairs: 1399, files: 9793, rows: 35,247,327,
coverage 2026-04-01..06-30}`; `ETHUSD_60.csv` = 2,184 rows, **zero gaps**; **`DOGUSD_60.csv`
exists**. Auth: none. The archive row *is* the store row (`timestamp→time`, `volume→volume`,
`trades→count`) — only `vwap` is missing. ~1 file + 1 ticker map + tests, and **no new project is
warranted**.

**Venue verdict, measured on two independent windows (720 + 2,183 aligned 1h bars):**
- **Price is safe** — return-σ ratio 0.9998–1.0020, corr 0.9991–0.9996, ~5bp constant premium.
- **Volume is not** — Kraken base volume 9.6×/18.6× smaller, 43×/126× fewer prints. `obv` terminal
  off by 18–266×; fitted `NormalizationStats` puts eval observations at **max|z| = 50.7–64.5** vs
  2.6–3.1 in train.
- **But `volume_zscore_20` and `trade_count_zscore_20` are UNAFFECTED** (z = +4.29/+4.36 at the
  seam, inside their normal 4.35/4.32) — **rolling z-scores are scale-invariant**, so the audit
  over-warns here. This is the kind of specificity that must survive into the decision.

**RESEARCH-3's verdict on G4 (the highest-leverage finding in the whole audit):**
`backtest_model` is **deterministic** (`backtest.py:328` `deterministic=True`, `:534`), so **all**
observed spread is *training* variance; and `seed` already flows to `--seed`
(`model_matrix.py:1119-1121`) and is in `REQUIRED_BACKTEST_FIELDS` (`:136`). Therefore
paired/CRN analysis, Cliff's δ, Wilcoxon signed-rank, bootstrap CIs and a printed
**detection floor** are computable **from results that already exist — zero new runs.** ~30-60
lines in `summarize`/`cmd_report`.

**Power arithmetic from the audit's own 0.3/1.0/2.7pp spread:** σ = 1.2343pp,
MDD = 2.8016·SE ⇒ **N=3 → MDD 2.82pp (unresolvable); need 38 / 96 / 383 seeds per group** to
resolve 0.8 / 0.5 / 0.25pp. Paired-by-seed (CRN): ρ=0.7 → 29 seeds, ρ=0.9 → 10. The measured
effect being chased (friction) costs only ~0.25–0.8pp. **Today's design cannot resolve its own
effect** — and, per the inversion above, adding history does not fix that.

---

## 4. The decision the architect actually has to make

The three research streams point at genuinely different shapes, and the command allows exactly one
outcome:

| Option | Outcome type | Repo touched | Cost | Blocking caveat |
|---|---|---|---|---|
| **G1** path resolver + loud-on-broken + non-vacuous test | `IMPROVE-EXISTING` | this repo only | ~4 + ~30 lines | Coupled: `test_model_matrix.py:1416-1418` pins the warning text |
| **G2-R1** `vwap_close_gap_zscore_20` + `count_per_range` | `IMPROVE-EXISTING` | this repo only | ~2 expressions | None — venue-correct, historical, already-parsed columns |
| **G2-R2** `kraken-microstructure` sibling | `NEW-DATA-SOURCE` | **new repo** | largest | Venue seam + seed/record boundary discontinuity; must be feature-gated until seeded |
| **G3-R1** Kraken OHLCVT archive as the seeder source | `IMPROVE-EXISTING` | `kraken-deep-history` | ~1 file + ticker map | **Load-bearing unverified assumption:** archive `volume` vs REST `volume` bar-for-bar can never be compared today (REST's 720-bar window can never overlap the archive's end date) |
| **G4-R2** detection floor + paired tests in `tools/model_matrix.py` | `IMPROVE-EXISTING` | this repo only | ~30-60 lines, **no new runs** | Answers "can we conclude?", but is measurement tooling rather than a data vector |

**Three tensions the architect should resolve explicitly, not silently:**

1. **The highest-directness item (G1, 5/5) and the highest-leverage item (G4) are in different
   repos of scope.** The command allows one vector per pass. G1 fixes a silent data defect; G4
   determines whether anyone could have *detected* that fix helping. Shipping G1 without G4 means
   a correct fix with no way to measure it — which the command's own gate (magnitude evidence by
   column name) partly compensates for, but does not resolve.
2. **G3's fix is blocked by G1's second instance.** `data.py:1054`
   `MarketDataStore(Path(...))` omits `expanduser()` and `deep-history.example.yaml:81` ships a
   `~` path — so seeding the store is pointless until that line is fixed. Any decision that picks
   G3 must carry the `data.py:1054` fix with it, or explicitly scope it out.
3. **G4 is arguably not a data-pipeline vector at all** — it is measurement validity. AUDIT ranked
   it 5/5 on "can anything be concluded" but 3/5 on feeding features. Whether an audit whose
   stated subject is the *data pipeline* may end on the *measurement harness* is a scope call the
   architect must make and justify, not slide into.

**What is already ruled out, with reasons:** macro calendars, on-chain, research/academic
(`researcher-python` exists on disk, referenced nowhere), and text/news + social as standalone
gaps — both are **G1 sub-cases**, since their seams are `null` for the same reason funding's is.
CoinAPI/Kaiko/Amberdata (sales-gated), Kaggle/HuggingFace (stale, venue-mismatched, licence-varied),
CCXT (adds no history), Tardis.dev (right resource, wrong gap — flagged as a G2 lead), and
`/public/Spread` (no volume ⇒ no imbalance).