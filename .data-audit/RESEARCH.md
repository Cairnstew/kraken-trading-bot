# RESEARCH ASSEMBLY — kraken-trading-bot (pass 2, 2026-09-30)

Lead-assembled from the three per-researcher files written this pass. Each
researcher's file is the authoritative detail; this is the index + the
lead's cross-file notes for the architect.

## NOTE ON AUDIT RANKING (lead, cross-file)
Two audits landed on the `audit` task (the force-shutdown of the first
auditor raced its completion; the respawned auditor ran a fresh pass that
overwrote AUDIT.md). The current on-disk AUDIT.md is the FRESH audit whose
top-3 is:
  1. Sound the three existing signal seams (ticker-blind join,
     duplicate-hour ValueError, unbounded ffill, F&G absence==extreme-fear)
  2. Activate the dormant microstructure group from on-disk funding data
     (49->51 features, zero new API calls)
  3. Record the training window, then split it (since/until plumbed but
     called by nobody; provenance missing)
The researchers below were assigned from the EARLIER audit's top-3 (G1
window-recording, G2 feature-width staleness, G3 fetch reliability). All
three overlap the fresh audit's list (window-recording is fresh-#3; width
guard and fetch reliability are fresh runners-up #2-related and #5). The
architect should treat the fresh AUDIT.md as authoritative for the final
ranking and use these RESEARCH files as the sourcing/library evidence for
the corresponding candidates.

## Researcher index

### RESEARCH-1.md (452 lines) — G1: record training window / apply the shipped store seam
- Config: one `data_window: {since, until}` block after `market_data_store`
  (configs/default.yaml:97); accessor `window_bounds(cfg)` tolerating flat keys.
- Callers: train.py:185-194, backtest.py:138-147, export.py:166-176 add
  since=/until=; paper_trade.py:289-298 must deliberately NOT take since
  (it wants the fresh tail).
- Gotcha: `since` moves the FETCH cursor (data.py:437) -> pass since=None to
  the fetch leg when a store is set.
- Provenance: persist `data_window.{actual_start, actual_end, n_bars,
  interval_minutes, store, source, data_hash}` (sha256 of
  hash_pandas_object; no new dep) after prepare_episode — record the
  ACTUAL post-slice bounds, not the request.
- Seed/refresh: exact commands in §5; bot-dev-shell seed (market-data mode,
  not fallback-CSV). Scheduler = systemd.user timer (ONE pick); sibling
  NixOS module blocked (no `market` subcommand; DynamicUser store-root
  mismatch).
- No library needed. ccxt rejected for G1 (does not lift the 720-bar
  ceiling). SB3 ReplayBuffer in-memory, no dataset pinning.

### RESEARCH-2.md (441 lines) — G2: pin and guard the fitted feature width at load
- `feature_names` ALREADY EXISTS in npz (features.py:78,101-106,
  round-trip tested) — G2 is an assertion, not a format change.
- The paper_trade guard (paper_trade.py:325-338) is TAUTOLOGICAL for this
  threat (expected and actual both come from the same dropping normalize);
  do not port it to backtest; re-anchor it.
- Recommended: Layer 1 = NormalizationStats helper + once-per-ticker assert
  at features.py:298 in compute (gated loaded-not-fitted), covers all four
  consumers; set-mismatch hard-fail, order-mismatch warn. Layer 2 =
  n_features at train.py:238-240, checked at backtest.py:161 +
  paper_trade.py:187-188. Layer 3 = feature_fingerprint sha256 incl. the
  `_SIGNAL_COLUMNS` code constant.
- MLflow/ONNX/TensorFlow rejected with measured grounds (TF blocked on
  py3.14); pandera rejected as dep, adopt its error-report shape.

### RESEARCH-3.md (316 lines) — G3: fetch reliability (retry/backoff, throttle, tick-level cache)
- F1: Kraken signals rate limit in the HTTP BODY with status 200 —
  urllib3.Retry(429) never fires; hand-rolled ~25-line retry loop in
  kraken-python transport (0 new deps) is the only correct option.
- F2: KrakenManager.from_env(min_interval=...) is a silent NO-OP
  (auth.py:73); client_from_credentials honours it — 2-line fix.
- F3: throttle not load-bearing (3 calls/pair/60s is ~300x under the limit);
  the real cost is the same 720-bar window re-requested 60x/hour + 3 O(file)
  JSONL parses per tick.
- Traps: private() is nonce-signed — retry public() unconditionally,
  private() only pre-response, default off (double-place risk).
- Cache seams ranked: (1) merge_extra_features parsed-frame cache keyed
  (path,size,mtime_ns); (2) engine bar-close gate (3->1 calls/tick);
  (3) paper-trader append+tail reusing existing since param.
- Bonus bug: `until` silently dropped on the live (null-store) path
  (data.py:402-412) — one-line fix + test.
