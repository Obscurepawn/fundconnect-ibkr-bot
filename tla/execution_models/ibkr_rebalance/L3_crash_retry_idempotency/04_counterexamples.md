# 04_counterexamples — L3_CrashRetryIdempotency  (FROZEN at close)

## Counterexample 1 — Candidate L3-A: `NoDoubleOrderAcrossClientIds` (`reqOpenOrders()` misses open orders across `clientId`s)
- **Property violated**: `NoDoubleOrderAcrossClientIds`
- **Config & invocation**: `run_tlc.sh L3_CrashRetryIdempotency.tla L3_CrashRetryIdempotency_client_id.cfg --det` (41 states generated, 41 distinct, depth 7)
- **Classification**: `candidate` (`IbAsyncBrokerAdapter.get_open_orders` at `broker.py:338` calls `ib.reqOpenOrders()`, which returns only open orders bound to the active `clientId`, blinding `working_by_sym_side` to open orders from a prior crashed `clientId` or manual TWS `clientId = 0`)

### Deterministic TLC Trace (7 states)
1. **States 1–2 (`Init` -> `BuildBatch`, `run_num = 1`, `ActiveClientId = 26`)**: `pos_sell = 100`, `batch_sell_qty = 100`, `batch_buy_qty = 100`.
2. **State 3 (`PhaseAFillPartial`, `src/fundconnect_bot/execution/policy.py:1116-1122`)**: Run 1 sells `50` shares (`pos_sell = 50, cash_avail = 50`) and leaves `sell_orders = {[id |-> 1, client_id |-> 26, rem_qty |-> 50, status |-> "Working"]}` on TWS.
3. **State 4 (`CrashBeforePhaseASave`, `src/fundconnect_bot/execution/policy.py:1122-1128`)**: Run 1 aborts before `save_execution_state` (`disk_completed = {}`).
4. **States 5–6 (`StartRetryRun2` -> `BuildBatch`, `run_num = 2`, `ActiveClientId = 27`)**: Run 2 connects on failover `clientId = 27`; `VisibleWorkingQty(sell_orders, 27) = 0` (`ib.reqOpenOrders()` hides `client_id = 26`'s working `50`-share order), so `BuildBatch` emits `batch_sell_qty = 50`.
5. **State 7 (`PhaseAFillFull`)**: Run 2 sells `50` shares (`pos_sell = 0`) while `client_id = 26`'s `50`-share sell order is **still `Working`** on TWS (`(100 - 0) + 50 = 150 > InitSellQty = 100`), violating `NoDoubleOrderAcrossClientIds` (risking an unintended `-50` short position!).

---

## Counterexample 2 — Candidate L3-B: `NoStrandedUnfilledTargetOnRetry` (Persisting `"WORKING"` / `"PARTIAL"` in `completed_intents` strands cancelled orders)
- **Property violated**: `NoStrandedUnfilledTargetOnRetry`
- **Config & invocation**: `run_tlc.sh L3_CrashRetryIdempotency.tla L3_CrashRetryIdempotency_stranded_working.cfg --det` (374 states generated, 347 distinct, depth 15)
- **Classification**: `candidate` (`execute_market_rebalance` at `policy.py:1125-1126, 1217-1218` persists non-terminal `"WORKING"` and `"PARTIAL"` statuses in `ExecutionState.completed_intents`; if TWS/venue later cancels/expires the order, `policy.py:536-537` permanently skips the intent on retry)

### Deterministic TLC Trace (15 states)
1. **States 1–5 (`Init` -> `BuildBatch` -> `PhaseAFillFull` -> `SavePhaseAAndProceedToFx` -> `FxAndCapBuys`)**: Run 1 (`clientId = 26`) sells all `100` Phase A shares (`pos_sell = 0, cash_avail = 100`) and sizes `capped_buy_qty = 100`.
2. **States 6–7 (`PhaseBStayWorking` -> `SavePhaseBAndFinishRun`, `src/fundconnect_bot/execution/policy.py:1208-1229`)**: Phase B buy remains `"WORKING"` after walk steps (`buy_orders = {[id |-> 1, client_id |-> 26, rem_qty |-> 100, status |-> "Working"]}`), and `policy.py:1217-1218` persists `disk_completed = {"SELL_INTENT", "BUY_INTENT"}`.
3. **State 8 (`InterRunTwsCancelWorking`)**: TWS / venue cancels the open order (`ApiCancelled` / `DAY` close -> `buy_orders = {[id |-> 1, ..., rem_qty |-> 0, status |-> "Cancelled"]}`, `pos_buy = 0`, `cash_avail = 100`).
4. **States 9–15 (`StartRetryRun2` -> `BuildBatch` -> `PhaseASkip` -> `SavePhaseAAndProceedToFx` -> `FxAndCapBuys` -> `PhaseBSkip` -> `SavePhaseBAndFinishRun`)**: Operator re-runs `execute` (`run_num = 2`); `BuildBatch` (`policy.py:536-537`) sees `"BUY_INTENT" \in disk_completed` and sets `batch_buy_qty = 0`, reaching `RUN2_DONE` with `pos_sell = 0`, `TotalWorkingBuy = 0`, `cash_avail = 100`, and `pos_buy = 0 < TargetBuyQty = 100`.

---

## Counterexample 3 — Candidate L3-C: `NoScaledBuyPermanentUnderfill` (`apply_cash_cap_to_buys` preserves `intent_id` when down-scaling buy quantity)
- **Property violated**: `NoScaledBuyPermanentUnderfill`
- **Config & invocation**: `run_tlc.sh L3_CrashRetryIdempotency.tla L3_CrashRetryIdempotency_scaled_buy.cfg --det` (380 states generated, 351 distinct, depth 15)
- **Classification**: `candidate` (`apply_cash_cap_to_buys` at `policy.py:1005` executes `capped.append(replace(o, quantity=scaled_qty))` without changing `o.intent_id` or recording that `scaled_qty < o.quantity`; when the scaled order reaches `"FILLED"`, `policy.py:1217-1218` burns `o.intent_id` in `completed_intents`, permanently blocking the remaining target shares on retry even after Phase A finishes filling)

### Deterministic TLC Trace (15 states)
1. **States 1–4 (`Init` -> `BuildBatch` -> `PhaseAFillPartial` -> `SavePhaseAAndProceedToFx`)**: Run 1 sells `50` of `100` Phase A shares (`pos_sell = 50, cash_avail = 50`, remaining `50` `Working` on TWS).
2. **State 5 (`FxAndCapBuys`, `src/fundconnect_bot/execution/policy.py:990-1008`)**: `apply_cash_cap_to_buys` scales `batch_buy_qty = 100` down to `capped_buy_qty = 50` (`run1_scaled_buy = TRUE`) while retaining `"BUY_INTENT"`.
3. **States 6–7 (`PhaseBFill` -> `SavePhaseBAndFinishRun`, `src/fundconnect_bot/execution/policy.py:1208-1229`)**: The `50`-share scaled buy fills (`phase_b_status = "FILLED"`, `pos_buy = 50, cash_avail = 0`), persisting `"BUY_INTENT"` in `disk_completed`.
4. **State 8 (`InterRunTwsFillWorking`)**: The remaining `50` shares of Phase A sell fill on TWS (`pos_sell = 0, cash_avail = 50`).
5. **States 9–15 (`StartRetryRun2` -> ... -> `SavePhaseBAndFinishRun`)**: Run 2 sees `"BUY_INTENT" \in disk_completed` (`policy.py:536-537`) and emits `batch_buy_qty = 0`, finishing at `RUN2_DONE` with `pos_sell = 0`, `cash_avail = 50`, `TotalWorkingBuy = 0`, and `pos_buy = 50 < TargetBuyQty = 100`.

---

## Counterexample 4 — Candidate L3-D: `NoDuplicateFxConversionOnRetry` (Unrecorded `bootstrap_fx_usd` submits duplicate FX conversion on retry)
- **Property violated**: `NoDuplicateFxConversionOnRetry`
- **Config & invocation**: `run_tlc.sh L3_CrashRetryIdempotency.tla L3_CrashRetryIdempotency_dup_fx.cfg --det` (237 states generated, 225 distinct, depth 12)
- **Classification**: `candidate` (`execute_market_rebalance` at `policy.py:1145-1148` submits `broker.convert_usd_to_hkd(bootstrap_fx_usd)` without checking or persisting FX completion in `ExecutionState`)

### Deterministic TLC Trace (12 states)
1. **States 1–5 (`Init` -> `BuildBatch` -> `PhaseAFillFull` -> `SavePhaseAAndProceedToFx` -> `FxAndCapBuys`, `fx_cfg = "WITH_FX"`)**: Run 1 sells `100` `SGOV.US` (`cash_avail = 100`), converts `BootstrapFxUsd = 30` (`fx_converted_total = 30, cash_avail = 70`), and caps Phase B buy at `70`.
2. **States 6–7 (`PhaseBStayWorking` -> `CrashBeforePhaseBSave`)**: Phase B `70`-share buy stays `Working` and Run 1 is interrupted before `save_execution_state` (note: even with `SavePhaseBAndFinishRun`, `ExecutionState` still contains no FX record!).
3. **States 8–12 (`StartRetryRun2` -> `BuildBatch` -> `PhaseASkip` -> `SavePhaseAAndProceedToFx` -> `FxAndCapBuys`)**: Operator re-runs the CLI command (`--bootstrap-fx-usd 30 --allow-fx-convert`); `FxAndCapBuys` (`policy.py:1145-1148`) sees `cash_avail = 70 >= 30` and submits a **second** `30` USD conversion (`fx_converted_total = 60 > BootstrapFxUsd = 30`).

---

## Stage-1 Adversarial Review Dispositions (2026-11-15)
- **Candidate L3-A (`NoDoubleOrderAcrossClientIds`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): IbAsyncBrokerAdapter.get_open_orders calls ib.reqOpenOrders() instead of ib.reqAllOpenOrders(), missing working orders across clientIds (src/fundconnect_bot/execution/broker.py:336-369).`
- **Candidate L3-B (`NoStrandedUnfilledTargetOnRetry`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): persisting non-terminal WORKING and PARTIAL in ExecutionState.completed_intents permanently strands unfilled shares on retry after TWS cancels/expires the order (src/fundconnect_bot/execution/policy.py:535-537, 1125-1126, 1217-1218).`
- **Candidate L3-C (`NoScaledBuyPermanentUnderfill`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): apply_cash_cap_to_buys down-scales quantity via replace(o, quantity=scaled_qty) while keeping o.intent_id, burning o.intent_id in completed_intents and permanently blocking remaining shares on retry after Phase A finishes filling (src/fundconnect_bot/execution/policy.py:1005, 1217-1218).`
- **Candidate L3-D (`NoDuplicateFxConversionOnRetry`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): bootstrap_fx_usd conversions are neither checked nor checkpointed in ExecutionState before Phase B, causing duplicate FX conversions on retry (src/fundconnect_bot/execution/policy.py:139-168, 1145-1154).`
