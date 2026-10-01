# DECISION — data-pipeline audit pass 2026-10-02

**Architect:** `architect`, team `data-audit-1002`.
**Inputs:** `.data-audit/AUDIT.md` (513 lines), `.data-audit/RESEARCH.md` (lead synthesis),
`.data-audit/RESEARCH-1.md` (G1), `.data-audit/RESEARCH-2.md` (G2), `.data-audit/RESEARCH-3.md` (G3+G4).
**Repo state:** `kraken-trading-bot` @ `444fe1f`.

---

## 1. THE DECISION

| | |
|---|---|
| **Outcome type** | **`IMPROVE-EXISTING`** |
| **Gap** | **G1** — the exogenous-signal seam ships with a path its own reader cannot resolve |
| **Scope** | This repo (`kraken-trading-bot`) **and** one sibling (`kraken-funding-rates`) |
| **New repo?** | **No.** No `NEW-DATA-SOURCE`, no naming step, no sibling directory, no submodule. |
| **Touched units** | `kraken_trading_bot/rl/data.py`, `kraken_trading_bot/rl/train.py`, `kraken_trading_bot/rl/export.py`, `configs/default.yaml` (comments only), `tests/test_rl_signal_config_wiring.py`, `tests/test_model_matrix.py` (lockstep only), `kraken-funding-rates/kraken_funding_rates/models.py` |

**The one-line version:** make the already-built, already-wired, already-allow-listed signal seam
actually read the file `configs/default.yaml` tells it to read, and make it *fail loudly* the next
time it cannot — instead of silently narrowing the observation.

---

## 2. WHICH EVIDENCE I RELIED ON

The three researchers corrected `AUDIT.md` six times. **Where they disagree with the audit I relied
on the researcher**, and specifically on these four:

1. **G1's failure mode is ABSENCE, not zero-fill (RESEARCH-1 §F4).** The audit says twelve columns are
   "permanently zero" and `signal_observed == 0.0` on every bar. Measured: the columns are **absent
   from the observation**, and `signal_observed` **is not in the observation at all**, because the
   freshness pair is written by the same seam (`data.py:487`) that the missing file skipped. So the
   diagnostic designed to make absence auditable *inside* the observation is itself absent. This is
   worse than the audit describes and it is why nothing downstream flagged it. Verified in source:
   `_add_signals_features` (`features.py:587+`) skips any column not in `df.columns`, and
   `data.py:368` returns `df` unchanged.
2. **There is no width signal at all.** Width as-shipped is **identical** to width-with-the-broken-
   path-configured. `check_feature_width` (`features.py:262`) compares **by name** and is genuinely
   non-self-referential, so it *would* fire on a 52-trained model replayed against a 59-wide
   pipeline — but nothing anywhere records the width the shipped config is *supposed* to produce, and
   `models/` is empty but for `.gitkeep`, so the guard is not even in the loop today. Verified: `ls -A
   models` → `.gitkeep`.
3. **`/public/Spread` is not backfillable (RESEARCH-2 §1.2), and `/public/Depth` never was.** The
   audit's Depth-vs-Spread contrast is half wrong: the real asymmetry is **Depth vs Trades**. This
   does not change the decision (G2 loses on other grounds — §6) but it removes Spread from any
   "cheap live source" argument.
4. **G4's intuition is inverted (RESEARCH-3 §4.3).** σ(L) ∝ √L — a longer eval window is a *noisier*
   measurement, and one 456-bar window is worth **~3 effective independent draws**. So depth buys σ
   *measurement*, not σ reduction. This is load-bearing for §6: it is why G3 loses as the runner-up
   and why G4 alone does not lower the detection floor.

**Feature width is disputed three ways and I am not quoting any of it.** Prior RUN LOG says
49/52/60; RESEARCH-1 *measured* 52/52/59; RESEARCH-2 *computed* 51/54/67 and **could not run a probe
at all** (its venv fails to import numpy, `libz.so.1` missing). RESEARCH-1's own denominators are
also internally inconsistent — `_SIGNAL_COLUMNS` (`features.py:46-77`) holds **19** entries, which I
counted directly, while RESEARCH-1 reports "3 / 18" and "10 / 18". **The width must be re-derived
from the real pipeline.** The *direction* is not in doubt: see §8.

---

## 3. WHY G1 — AND WHY IT IS NOT A FOOTNOTE

The brief is explicit that the highest-directness `IMPROVE-EXISTING` item must be a real candidate.
It wins on every axis the audit ranked, and it wins on two the audit did not rank:

**Against the audit's own ranking key (directness = how directly the fix lands in the RL observation):**
G1 is **5/5** and is the only gap whose payoff requires **zero feature-engineering work** — the
columns are already in `_SIGNAL_COLUMNS`, already forwarded by the `signals` feature group, already
counted in the fitted allow-list. AUDIT.md:214-220 says so explicitly; I verified
`features.py:46-77` and `features.py:587-605`.

**It is the only gap whose root cause is a defect in this repo, reproduced and CWD-independent.**
`data.py:363` is `path = Path(extra_features_file)` with no `expanduser()`. `yaml.safe_load`
preserves the `~`, `Path("~/x")` looks for a directory literally named `~` under the CWD. Verified
in source at `data.py:363-368`, and the shipped value at `default.yaml:79` is
`funding_features_file: ~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl`. This is wrong
from **every** CWD. **The correct pattern already exists in the same repo** — `backtest.py:219` is
`Path(config_path).expanduser()`. This is an internal inconsistency, not an inter-project contract
failure, and no other repo can fix it.

**It is keyless and it is free.** The funding sibling's own service file says "Keyless (public Kraken
Futures ticker)". No rate limit, no paid tier, no licence question, no new credential, no
ToS-sensitive surface. Nothing to justify the cost of.

**It is the smallest honest change available:** a shared path helper plus a gate change, ~4 + ~30
lines. RESEARCH-1's sequencing note is the reason it beats G2-R1: *"R1+R2 alone convert a silent
52-wide pipeline into a loud failure. That is the whole of G1's risk."*

**It is the prerequisite for trusting every later data fix.** A seam that silently returns `df` on a
broken path makes any subsequent feature-width change unobservable. Making it loud first is what
turns the next pass's fix into something a gate can check.

**It is also the only item on the board with a cheap secondary payoff:** `data.py:1054`
`MarketDataStore(Path(market_data_store))` is the **same one-line defect in a second instance**, and
`deep-history.example.yaml:81` ships `~/Projects/kraken-market-data/store`. Fixing G1 therefore
**unblocks G3 for the price of four lines** — I verified both sites directly. I take that fix as part
of G1's scope and explicitly **do not** take G3 itself (§6).

---

## 4. THE REAL SHAPE OF THE RL PIPELINE, AND THE EXACT LANDING POINT

**Config inputs.** One YAML (`configs/default.yaml`) is read by `train`, `backtest`, `paper-trade`
and `export-data` alike: `feature_windows: [1, 4, 24]` and `feature_groups: ["price", "technical",
"volume", "microstructure", "signals"]` (`default.yaml:43,47`), the three exogenous path keys
(`extra_features_file`, `funding_features_file`, `social_features_file` — `:57,79,87`), the staleness
bound `signal_max_age_hours: 12` (`:126`), the ticker guard `signal_require_ticker: true` (`:118`),
and the store key `market_data_store: null` (`:142`).

**The `train`/`backtest` knobs.** `--ticker`, `--model`, `--config`, `--pages` ("each ~720 candles"),
`--episode-bars`, `--timesteps`, `--seed`, `--interval-minutes`, `--action-space`,
`--initial-balance`, `--fee-rate`, `--slippage`, `--models-root`, `--json`; `backtest` takes
`--ticker`, `--model`, `--pages`, `--seed`, `--config`, `--json`, `--models-root`. **Note
`--seed` flows to `train`, not to `backtest`** (`model_matrix.py:1119-1121` vs `:1132-1136`) —
`backtest_model` is deterministic (`backtest.py:328,534`), so replay variance is zero and all
observed spread is training variance. This is RESEARCH-3's reframe and it is correct, but it is
G4's territory, not G1's.

**The artifact layout.** `models/{TICKER_ID}/{model_name}/` — verified at `registry.py:4,136-153`,
`ticker_id` normalised via `normalize_ticker_id`, created by `register_model`
(`registry.py:160-189`) at `train.py:283`. Each directory holds `model.zip`, `normalization.npz`
(written at `train.py:281` by `features.save_normalization`) and `config.yaml` (which also carries
`n_bars`, `train.py:277`). **`normalization.npz` is where the fix becomes observable**: it stores
`feature_names`, which is exactly what `check_feature_width` compares against.

**The consumption row.** `candles_to_dataframe` (`data.py:691-727`) emits exactly
`time, open, high, low, close, vwap, volume, count`, indexed by UTC `DatetimeIndex`, sorted oldest
first, raising `NotEnoughDataError` on an empty list. `add_derived_ohlcv_features`
(`data.py:627-688`) extends it in place-presence-gated with `vwap_dev = close/vwap − 1`,
`trade_count_zscore_20` (rolling 20, `ddof=0`) and `volume_per_trade = volume/count`. One row per
`(ticker, bar_ts)`.

**Where the feature or fix is read, per ticker.** **The single landing point is
`merge_extra_features` at `data.py:360-368`** — the `if not extra_features_file:` /
`path = Path(...)` / `if not path.is_file():` block, plus the `if not records:` block at
`data.py:382-384`. It is reached from `read_ohlc_dataframe` on **both** branches —
`data.py:853-860` (live fetch) and `data.py:1009-1016` (store-backed) — which already thread the same
three keys, so **live/store symmetry is preserved for free**.

Downstream of that seam, per bar: `FeaturePipeline.compute` (`features.py:447`) assembles the five
groups; `_add_microstructure_features` (`features.py:573-585`) derives `spread` from `bid`/`ask` —
**so today the `microstructure` group produces no columns at all**, because its only inputs come from
the funding record that cannot be read; and `environment._raw_feature_array` (`environment.py:470`)
does `ffill().fillna(0.0)` → `NormalizationStats.normalize` → `to_numpy(dtype=np.float32)`,
**consumed positionally by PPO**.

**The config key: `funding_features_file`. No new key is added.** The change reads the three keys
that already exist. That is deliberate — it keeps the fix on the seam the pipeline already owns.

---

## 5. EXACT SCOPE — THE IN-SCOPE UNITS

### 5.1 This repo: `kraken-trading-bot`

**`kraken_trading_bot/rl/data.py`** — one shared private helper beside the existing private helpers
(`_resolve_store` at `:1020`, `_filter_ticker` at `:504`):

```
_resolve_config_path(value) -> Path | None
    None / ""  -> None
    Path(str(value)).expanduser()
```

Applied at **five** sites, which is the whole of RESEARCH-1's ranking #1–#5:

| site | key | why |
|---|---|---|
| `data.py:363` | the three signal keys | **G1 itself** |
| `data.py:1054` | `market_data_store` | same bug; **unblocks G3** |
| `train.py:82` | `--config` | silent `{}` today — adopt `backtest.py:219`'s raise semantics |
| `export.py:250` | export CSV root | default is relative; harmless, but prevents instance #6 |
| `export.py:263` | export CSV destination | same |

(`features.py:184,210` — the `.npz` parents — I **exclude**: the parent is already expanded, so
`.expanduser()` is a no-op and the site cannot misbehave. RESEARCH-1 ranks it P3.)

**`kraken_trading_bot/rl/data.py:360-368` and `:382-384`** — the gate, in order:

1. `null` / empty ⇒ `return df`, **silently. UNCHANGED.** First-run UX preserved.
2. non-null but not a file ⇒ **raise**, naming the config key, the **raw** value, the **expanded**
   value (so the `~` defect is legible if it ever recurs), and the producer command
   (`just funding-pull` / `just funding-timer`). `backtest.py:218-224` is the in-repo precedent,
   and its docstring states the reasoning verbatim.
3. resolves but **0 records** ⇒ raise as well, and promote `data.py:383` from `DEBUG` to `WARNING`
   regardless. A file that exists and is empty is a **producer** failure, not a first-run state.
4. present, no ticker overlap ⇒ **keep today's WARNING at `data.py:477-486` verbatim.** This is the
   legitimately-expected state for a young forward-only log and must not become fatal.

**`tests/test_rl_signal_config_wiring.py:711-734`** — **replace the body** of
`test_config_points_at_a_real_funding_file_with_a_12h_bound`. Verified: its assertions are
`assert funding` + `str(funding).endswith(".jsonl")` + two `== 12` checks — it never calls
`expanduser()`, `is_file()`, or the seam, so it **passes for `/definitely/not/here/nope.jsonl`**.
Keep the `signal_max_age_hours == 12` assertions (they are fine); replace the path assertion with
the fake-`HOME` test in §5.3. Then **add** the producer-record-shape test (RESEARCH-1 §5(E)):
construct from `FundingSnapshot`'s real key set and assert the ticker guard fires on a cross-ticker
file.

**`tests/test_model_matrix.py:1416-1418`** — **lockstep, not optional.** See trap #1.

**`configs/default.yaml` — comments only, no value changes.** Delete the false
`see nix/module.nix` cross-reference at `:73-74` (verified: `nix/module.nix` is a NixOS module whose
implementation sets only `environment.systemPackages` and one `systemd.services` unit — it contains
**no timer and no `systemd.user.*`**, which will not evaluate there). Correct the `49 → 55` record
in `features.py:270` ("the width guard that ships with the 49 -> 55 widening") and the
`301 of 721` claim at `default.yaml:104-107`. **No config value changes in this pass.**

### 5.2 Sibling: `kraken-funding-rates` — **required, not optional**

`kraken_funding_rates/models.py:50-65` — add `"ticker": self.symbol` to `FundingSnapshot.to_dict()`.
Verified: the record currently carries `symbol, spot_pair, timestamp, funding_rate,
funding_rate_prediction, mark_price, index_price, basis, open_interest, bid, ask, vol24h` and **no
`ticker`** — while both sibling producers emit it (`ticker-news-signals/…/models.py:96`,
`kraken-social-signals/…/models.py:162`).

**Why this is in scope for G1 and not a separate item:** G1's entire job is to make the seam sound.
Shipping the resolver **without** this would *switch on* the one channel whose records carry no
ticker — i.e. it would arm `signal_require_ticker: true` as inert exactly where it matters. Verified:
`_TICKER_FIELD = "ticker"` (`data.py:111`) and `_filter_ticker` (`data.py:504-545`) falls through to
"treat it as a one-ticker file and merge every record" with a WARNING when the field is absent. And
`default.yaml:78` actively invites the failure — *"Point a second pair's model at its own file, e.g.
`signals/sol_usd_funding.jsonl`"* — so a SOL model pointed at the ETH file merges ETH funding onto
SOL bars silently. **R1–R3 without this line is a half-built outcome.**

### 5.3 The smallest adapter that proves the seam

One fake-`HOME` (`monkeypatch.setenv("HOME", tmp_path)`), one `funding_features_file` spelled
`~/signals/f.jsonl`, one materialised JSONL record, one call to `merge_extra_features`, then:

> assert `"signal_observed" in result.columns` **and** `bool(result["signal_observed"].any())`

i.e. assert the seam **consumed** the file. This fails on `data.py:363` today (the column is
*absent*, not zero) and passes only with `expanduser()`. That is the non-vacuity proof, stated as a
contrast: the shipped test passes for a path that does not exist; this one cannot.

---

## 6. RUNNER-UPS, AND WHY THEY LOST

### Runner-up #1 — **G4: make power a measurement instead of a literal** (`tools/model_matrix.py`)

*Highest leverage per line in the entire audit* (RESEARCH-3 §8.3: ~30-60 lines, **no new runs**).
It lost on **scope**, and I accept that call against myself:

- **It is measurement validity, not a data vector.** AUDIT ranked it 3/5 to the feature space and 5/5
  to "can anything be concluded". This audit's stated subject is the data pipeline. It puts no new bar
  of data in front of the agent.
- **It would ship unexercised.** `models/` is empty but for `.gitkeep`; RESEARCH-3 §7 lists ρ (the
  pairing gain) as **unmeasured**, and it is measurable only from a paired run that does not exist.
  So the headline deliverable — paired Δ, Wilcoxon, Cliff's δ, bootstrap CI — would land behind
  **N = 0 groups**. RESEARCH-3 §4.6's own honest output at N=3 is a *negative* result. Shipping a
  detection-floor printer with nothing to print is the "completes without error" trap in a new guise.
- **It does not lower the floor.** σ_train = 1.2343 pp is PPO's own variance; no data change touches
  it. RESEARCH-3 §4.4: adding *windows* helps far less than adding *seeds* (K=1→24 buys 28%; N=3→10
  buys 60%). And per the §2.4 inversion, depth buys σ *measurement*, not σ reduction — so G4's
  cheapest honest output today is "this design resolves nothing below 2.82 pp", which is a **finding
  about the harness**, not a fix to the pipeline.

**This is the right next pass.** Its §4.5 output contract consumes only `REQUIRED_BACKTEST_FIELDS`,
which already carries `seed` — so it should be scheduled immediately after G1, and G1's
column-by-name gate (§8) is deliberately designed to be readable without it.

### Runner-up #2 — **G2-R1: ship `vwap_close_gap_zscore_20` + `count_per_range`**

The cheapest *additive* feature work on the board: two rolling expressions over `vwap` and `count`,
which `data.py:717,719` already parse and `data.py:627-688` already derive from. Keyless,
venue-correct (Kraken spot), fully historical, **no new seam, no new source, no coupling** — it loses
on **ordering**, not on merit:

- It widens an observation whose exogenous half is **entirely absent and unmeasurable**. Adding two
  columns while fifteen allow-listed signal columns silently do not exist is the wrong sequence: it
  makes the observation *wider* without making it *truer*, and it widens the very surface
  `check_feature_width` is comparing by name.
- Per RESEARCH-3 §4.6, nothing below 2.82 pp is resolvable on the current design — so these two
  columns could not be *shown* to help either. Unmeasurable additions to a 3-seed harness are
  indistinguishable from noise.
- It is genuinely orthogonal and cheap: **not wrong, second.**

### Runner-up #3 — **G3: swap `kraken-deep-history`'s seeder to Kraken's own OHLCVT archive**

Researched and verified by download (RESEARCH-3 §A1: `Kraken_OHLCVT_2026Q2.zip` = 537,866,903 B,
`MANIFEST.json` = 1,399 pairs / 9,793 files / 35,247,327 rows, `ETHUSD_60.csv` zero gaps,
`DOGUSD_60.csv` exists). It discharges the audit's venue caveat **completely** and supplies DOGE/USD.
It lost **this** pass on four counts:

1. **It is blocked by G1's second instance.** Choosing G3 pays `data.py:1054` anyway — so it would
   carry G1's cost without G1's 5/5 directness payoff.
2. **Its load-bearing assumption cannot be verified with today's tooling.** Archive `volume` vs REST
   `volume`, bar for bar: the Q2 archive ends 2026-06-30 and REST serves only the most recent 720
   bars, so **there is no overlap to compare and none is possible** (RESEARCH-3 §7). A Phase 6 gate
   for G3 cannot be written. G1's gate can.
3. It costs a 538 MB/quarter (10.5 GB full) download and a store seed.
4. **Its payoff is orthogonal, not additive.** On a pinned single window, depth contributes **zero
   variance reduction** — every cell already sees the same bars (RESEARCH-3 §5.2). Depth's value is
   σ_win, which needs walk-forward (G8) to measure at all.

**G1 unblocks G3 at no extra cost. G3 deserves its own pass.**

### Runner-up #4 — **G2-R2: a `kraken-microstructure` sibling** (`NEW-DATA-SOURCE`)

The only genuine `NEW-DATA-SOURCE` candidate, and the reason the brief's warning not to default to it
matters: it is also the **largest** and the only one carrying **two undecided discontinuities** I
would have to decide rather than discover (RESEARCH-2 §5.8, RESEARCH.md §2):

- The archive is Binance **USD-M perpetual futures** under a Kraken spot label — the *same* venue
  hazard G3 exists to remove, and RESEARCH-3 already quantified its damage for the analogous case
  (`obv` terminal off 266×; fitted `NormalizationStats` putting eval observations at **max|z| =
  50.7–64.5** vs 2.6–3.1 in train).
- WS `depth=10` (tick-based) vs `bookDepth` ±0.2 % (band-based) are different constructions, so the
  seeder and the forward recorder **will not match at the boundary**.

Building it while G3's venue hazard is unfixed repeats the mistake. And RESEARCH-2 §1.3 is decisive
that a forward-only tape is an *instrument*, not a feature: 456 bars is **19 days of uninterrupted
uptime**, and one 13-hour outage silently zero-fills the column via the bounded carry at
`data.py:449-463` — reproducing G1 in a second group. **Not chosen.**

---

## 7. WHAT I AM EXPLICITLY **NOT** DOING

- **No `NEW-DATA-SOURCE`.** No new repo, no `../kraken-*/` directory, no naming step. The available
  keyless depth (Kraken OHLCVT) and the available keyless L2 (Binance Vision) both belong in
  *existing* siblings; neither justifies a new project, and RESEARCH-3 §6 states the house style for
  the archive swap is a like-for-like module substitution inside `kraken-deep-history`.
- **G3's archive swap.** Out of scope; unblocked by this pass and owed its own.
- **G4's detection floor / paired tests.** Out of scope; scheduled next (§6).
- **G2-R1's two new features.** Out of scope; second (§6).
- **G2-R2, G5 (retry/backoff), G6, G7, G8 (walk-forward).** Out of scope. G5 in particular is
  RESEARCH-3's stated **precondition** for any long push-down, so it must land *with* G3, not inside
  this pass.
- **No config value changes.** `extra_features_file` and `social_features_file` stay `null`;
  `market_data_store` stays `null`; `signal_max_age_hours` stays 12. The two `null` channels are
  first-run-friendly by design and the raise is keyed on **non-null**, so no new user's first
  `train` breaks.
- **The ops/timer work (RESEARCH-1's R5).** `just funding-timer`, adding news + social units, and
  deleting the committed rendered `systemd/kraken-trading-bot-funding.service` (verified to be a
  `diff`-identical, machine-specific render of the `.in` template, committed with `/home/seanc/…`
  hardcoded in `ExecStart=`) — this is **host state mutation**, not a pipeline fix, and it is a
  **documented prerequisite for the Phase 6 gate**, not a build item. RESEARCH-1 §8 also leaves two
  questions open that need a live read (do the news/social producers append or overwrite; is the
  funding console script reachable from a second machine's `systemd` `ExecStart`).
- **No real API key anywhere.** The funding pull is keyless by design; nothing in this plan adds a
  credential.

---

## 8. ACCEPTANCE SIGNAL FOR PHASE 6'S GATE

**"Completes without error" is explicitly NOT sufficient.** A prior pass had a run that completed
cleanly while silently collapsing to 1 bar / 0 trades — and the feature-WIDTH guard still **PASSED**,
because it checks width, not NaN. Four conjunctive conditions, all nameable:

**(a) The seam is *consumed*, by name — not merely read.**
The non-vacuous test of §5.3 passes **on a `~`-spelled path under a fake `HOME`**. Fails today.
Additionally, with a real funding record present, `merge_extra_features` must return a frame in
which `signal_observed` **is present** and `.any()` is **true**, and `signal_age_hours` is present.
(Under the shipped config today, `signal_observed` is **absent from the observation entirely** — so
asserting `== 0.0` anywhere is asserting the *bug*.)

**(b) New columns present BY NAME in the fitted artifact.**
Read `feature_names` out of
`models/{TICKER_ID}/{model_name}/normalization.npz` and assert these **7 names are present**:
`basis`, `funding_rate`, `funding_rate_prediction`, `signal_age_hours`, `signal_observed`, `vol24h`
(merged verbatim by the seam) **and `spread`** (built by `_add_microstructure_features`,
`features.py:577-580`, which today produces **nothing at all** because its only inputs `bid`/`ask`
come from the unreadable funding record). Assert by name, not by count.
**The width number must be re-derived from the artifact — do not hardcode it.** Three sets are in
circulation (RUN LOG 49/52/60; RESEARCH-1 measured 52/52/59; RESEARCH-2 computed 51/54/67 and could
not run a probe) and RESEARCH-1's denominators do not even match `_SIGNAL_COLUMNS`' 19 entries.
**Expected *direction* of change: width increases relative to the shipped default**, by roughly the
seven names above, with `feature_windows: [1, 4, 24]` unchanged.

**(c) Magnitude evidence — bars replayed and trades taken, from the same run.**
`backtest --json` already emits exactly the fields needed (`REQUIRED_BACKTEST_FIELDS`,
`tools/model_matrix.py:124-143`). The gate must assert, on the **same** invocation that produces (b):

| field | threshold | why |
|---|---|---|
| `n_bars` | **> 1** — and reported as a count, not just "positive" | the magnitude guard `backtest.py:147` exists for; the prior pass's failure was a clean run at **1 bar** |
| `num_trades` | **> 0**, and the count printed | a 0-trade replay passes every existing check |
| `n_bars` vs the pinned window | `n_bars` consistent with the requested span at the configured `ohlcv_interval_minutes`, **not** silently short | the `pages`-undershoot silent-short-window failure (RESEARCH-3 §3.1) |

**Do not assert on `excess_return` sign or magnitude.** Per G4 that quantity is not resolvable at
this design; a gate that keys on it would be a coin flip.

**(d) The negative direction is asserted too — the fix must not become a false failure.**
With `extra_features_file: null` and `social_features_file: null` (the shipped default), a fresh
clone with no sibling repos cloned must still run `train` **cleanly and silently**. And with
`funding_features_file` non-null but unresolvable, it must now **raise by name**, naming the key, the
raw value and the expanded value. Both directions falsifiable.

**Suite state.** `tests/test_model_matrix.py:1416-1418` must be updated **in the same commit**, or
the fix reads as a red suite and invites a revert (trap #1).

---

## 9. COUPLING TRAPS A BUILDER MUST RESPECT

1. **`tests/test_model_matrix.py:1416-1418` pins the bug's own warning text.** Verified: the
   `REAL_ACTION_SPACE_MISMATCH` fixture contains the verbatim line
   `"Extra features file not found: ... -- skipping signal merge"`, captured from a real run. When
   item (d) makes that state a **raise**, the string stops reaching stderr and the fixture stops
   matching. **Move it in lockstep in the same commit**, or the suite goes red and the fix looks
   wrong.
2. **The two `null` channels must stay silent.** `extra_features_file: null` and
   `social_features_file: null` (`default.yaml:57,87`) must keep the plain `return df`. Key the raise
   on **non-null only** — a non-null key is a declared intent to use it. Getting this backwards
   breaks the first-run experience for a new user, which is the one real cost RESEARCH-1 §3
   identifies against Option 1.
3. **Do NOT add a constant-column or all-zero guard.** Under the shipped config the columns are
   **absent**, not zero, so an all-zero check fires on nothing; and after the fix `signal_observed` is
   legitimately constant for a young forward-only log, which would fail a "no constant columns" guard.
   Worse, `NormalizationStats.normalize` deliberately floors zero std (`features.py:218-232`), so
   constant features normalise to 0 and a marker column that is constant carries no information — it
   would itself be zero-filled by `_raw_feature_array` (`environment.py:487`). The single best
   non-vacuous variant is the **positive** assertion: `signal_observed.any()` when a seam file is
   configured.
4. **`_SIGNAL_COLUMNS` (`features.py:46-77`) is the single definition** — imported at
   `data.py:46-51`; `grep` finds no second literal. Do not add one. And `bid`/`ask`/`spread` are in
   `_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:101`): `_add_microstructure_features` is their
   **single writer**, `bid`/`ask` must not be whitelisted raw into the observation, and no tick-level
   recorder may compete for the name `spread` (`features.py:95-98` reserves `realized_spread_bps` for
   that).
5. **`data.py:1054` must not disturb the `null` path.** `data.py:949-961` is a byte-identical
   pass-through for `market_data_store: null`; adding `expanduser()` to `_resolve_store` must leave
   that exactly as it is, or every default-config run breaks.
6. **`systemd.user.*` does not belong in `nix/module.nix`.** That is a NixOS module
   (`{ config, lib, pkgs, … }`, keyed off `config.services.kraken-trading-bot`); `systemd.user.*` is not
   in its option namespace and will not evaluate. Delete the false `see nix/module.nix`
   cross-reference at `default.yaml:73-74` and leave the timer on the `justfile` path
   (`justfile:135-149`) that already works.
7. **A pre-ticker-tagged file is a supported state, not a bug.** `_filter_ticker`
   (`data.py:504-545`) intentionally treats a file with **no** `ticker` field as an explicitly
   one-ticker file and merges at WARNING, and `default.yaml:109-117` documents that. The consumer-side
   mitigation of accepting `symbol` as an alias is defensible as a **stopgap** but is the wrong layer;
   the permanent fix is the producer emitting `ticker`. Do not "fix" this by making the consumer
   stricter in this pass — that would break pre-ticker-tagged files and the documented one-ticker
   cron, and would turn the funding channel into a hard failure until §5.2 lands.
8. **`train` vs `backtest` config semantics differ today** (`train.py:80-88` returns `{}`;
   `backtest.py:218-224` raises). The backtest behaviour is the correct one — its docstring says so —
   so adopt it for `train`, and be aware this **changes `train`'s failure mode** from "silently train
   on CLI defaults" to "refuse". That is intended, and it will surface any spec that passes a bad
   `--config`.

---

## 10. PHASE 5 HANDOFF (one paragraph)

The integrator will land one shared `_resolve_config_path()` helper in `kraken_trading_bot/rl/data.py`
and apply it at five sites (`data.py:363`, `data.py:1054`, `train.py:82`, `export.py:250,263`);
change `merge_extra_features` so a `null` key stays silently off, a **non-null but unresolvable** path
**raises** naming key + raw + expanded value + producer command (matching `backtest.py:218-224`), a
resolvable-but-empty file raises and logs at `WARNING` instead of `DEBUG`, and the no-ticker-overlap
case keeps today's `WARNING`; add `"ticker": self.symbol` to `FundingSnapshot.to_dict()` in the
`kraken-funding-rates` sibling so `signal_require_ticker: true` is no longer inert on the one
configured channel; replace the vacuous `test_config_points_at_a_real_funding_file_with_a_12h_bound`
with a fake-`HOME` + `~`-path test that asserts the seam **consumed** the file
(`"signal_observed" in df.columns and df["signal_observed"].any()`), add the producer-record-shape
cross-ticker test, and update `tests/test_model_matrix.py:1416-1418` in the same commit; and correct
the `49 → 55` / `301 of 721` / `see nix/module.nix` records without touching any config **value**.
The **output contract is unchanged** — no new column, no new producer, no new config key, no new repo:
the pipeline still consumes one row per `(ticker, bar_ts)` of `time, open, high, low, close, vwap,
volume, count` (`data.py:691-727`); the only thing that changes is that the existing producer's output
finally arrives at the existing seam. **Config key: `funding_features_file`** — the smallest adapter
that proves the seam is one fake-`HOME`, one `~`-spelled key, one materialised JSONL record, one
`merge_extra_features` call, one `.any()` assertion.

---

DECISION COMPLETE