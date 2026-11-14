# 06_notes — L3_CrashRetryIdempotency

## Candidate Bug Analysis & Suggested Minimal Fixes (for Phase 2 review)
1. **Candidate L3-A (`broker.py:336-369`)**:
   - Replace `ib.reqOpenOrders()` (`broker.py:338`) with `ib.reqAllOpenOrders()` so TWS returns open orders across **all** `clientId`s (`26`, failover `27`, and manual TWS GUI `0`).
   - Also in `get_open_orders` (`broker.py:340-355`): explicitly skip `str(trade.orderStatus.status or "") in ("Filled", "Cancelled", "ApiCancelled", "Inactive")` and avoid `float(trade.orderStatus.remaining or (tot - filled))` where `0.0` is falsy in Python (`use trade.orderStatus.remaining if trade.orderStatus.remaining is not None else (tot - filled)`).
2. **Candidate L3-B (`policy.py:1125-1126, 1217-1218`)**:
   - Only persist genuinely terminal fills (`if fr.status == "FILLED" and fr.filled_qty >= fr.requested_qty:`) in `completed_intents`, never `"WORKING"` or `"PARTIAL"`! While an order remains `WORKING` or `PARTIAL` on TWS, `get_open_orders()` (`reqAllOpenOrders()`) + `get_positions()` (`policy.py:442-448`) already deducts both filled shares (`cur_qty`) and open working shares (`working_qty`) so no duplicate order is placed; if TWS subsequently cancels/expires the order (`working_qty == 0`), a retry run cleanly submits the remaining `desired_qty - cur_qty` shares!
3. **Candidate L3-C (`policy.py:1005, 535-537, 1217-1218`)**:
   - Why L3-C is distinct from L3-B: when `apply_cash_cap_to_buys` (`policy.py:1005`) scales `o.quantity` down from `100` to `50`, the scaled order fills `50/50` (`fr.status == "FILLED"`), so filtering `fr.status == "FILLED"` alone still burns `o.intent_id`!
   - Minimal fix: in `apply_cash_cap_to_buys` (`policy.py:1005`), when `scaled_qty < o.quantity`, either derive a distinct capped intent ID (`replace(o, quantity=scaled_qty, intent_id=f"{o.intent_id}:cap:{scaled_qty}")`) OR record `"unscaled_qty": o.quantity` / do not mark the full-target `o.intent_id` completed when `scaled_qty < o.quantity` (relying on `cur_qty` from `get_positions()` so Run 2 sees `desired_qty - cur_qty = 100 - 50 = 50` once Phase A finishes filling!).
4. **Candidate L3-D (`policy.py:139-168, 1145-1148, 1128-1137`)**:
   - Persist FX conversion state in `ExecutionState` (e.g., record `"FX:USD.HKD"` in `completed_intents` with `converted_usd` and `status` — immediately checkpointing `save_execution_state` right after `broker.convert_usd_to_hkd` succeeds in `Filled`/`PreSubmitted`/`Submitted` state, and subtracting already-converted/working FX USD from `bootstrap_fx_usd` on retry!).

---

## Stage-1 Adversarial Review Update (2026-11-15)
- **Verdict**: `L3-A`, `L3-B`, `L3-C`, and `L3-D` all **CONFIRMED** (`@tla-reviewer` `#56`).
- Note on `L3-D` fix verification in `L3_CrashRetryIdempotency_v2.tla`: verify that `save_execution_state` is checkpointed **immediately** after `convert_usd_to_hkd` succeeds (before Phase B runs) so `CrashBeforePhaseBSave` cannot cause a duplicate `USD.HKD` conversion in Run 2!

## Fix validation (`2026-11-15` — `L3_CrashRetryIdempotency_v2`)
- **Disposition**: **retired(fixed, validated by `L3_CrashRetryIdempotency_v2`)** across `L3-A`, `L3-B`, `L3-C`, and `L3-D`.
- **Verified code changes modeled**:
  1. **`L3-A` (`src/fundconnect_bot/execution/broker.py:339-389`)**: `get_open_orders` invokes `ib.reqAllOpenOrders()` (`341-345`), filters `DoneStates` (`348-350`), normalizes `CASH` `USD.HKD` open orders to `"USD.HKD"` (`354-360`), and uses `raw_rem is not None` (`371-374`) so working equity and FX orders across all `clientId`s (`0`, `26`, `27`, `99`) are visible on retry.
  2. **`L3-B` (`src/fundconnect_bot/execution/policy.py:1140-1153, 1316-1329`)**: Both Phase A and Phase B state checkpoints record `completed[fr.intent_id]` strictly when `fr.status == "FILLED"` (never `"WORKING"` or `"PARTIAL"`).
  3. **`L3-C` (`src/fundconnect_bot/execution/policy.py:1005-1014, 1303-1304`)**: `apply_cash_cap_to_buys` assigns `intent_id=f"{o.intent_id}:cap:{o.quantity}:{scaled_qty}"` whenever `scaled_qty < o.quantity`, preserving the base `o.intent_id` for residual fills once Phase A completes while deduplicating exact repeat capped intents (`1303-1304`).
  4. **`L3-D` (`src/fundconnect_bot/execution/policy.py:1164-1247`)**: `execute_market_rebalance` checks `fx_done_in_state` (`fx_intent_id in completed`, `1165`) and `fx_open_working` (`1166-1169`), reserves `bootstrap_fx_usd` if an FX order is currently working (`1181`), and immediately calls `save_execution_state` (`1238-1247`) right after `convert_usd_to_hkd` succeeds **before** Phase B starts (`CrashBeforePhaseBSave`).
- **Residual advisory observation (outside `L3` crash boundaries)**:
  - `policy.py:1238-1247` + `fx_open_working` (`1166-1169`) closes all post-Phase-A, mid-Phase-B, open-FX, and post-Run-1 retries; a sub-second socket drop inside `convert_usd_to_hkd`'s `ib.sleep(1.0)` (`broker.py:588`) where the `FXCONV` order simultaneously fills before reconnect would require pre-setting `fx_order.orderRef = fx_intent_id` and checking `ib.executions()` (`reqExecutions`) if sub-call crash atomicity were ever required.
- **Before / after TLC summary**:
  - Pre-fix (`L3_CrashRetryIdempotency.tla`): violated `NoDoubleOrderAcrossClientIds` (`41` states), `NoStrandedUnfilledTargetOnRetry` (`347` distinct states), `NoScaledBuyPermanentUnderfill` (`351` distinct states), and `NoDuplicateFxConversionOnRetry` (`225` distinct states).
  - Post-fix (`L3_CrashRetryIdempotency_v2.tla`): **PASS** (`0` violations across all 5 invariants; `2,452` distinct / `3,012` generated states in `_v2.cfg`, `3,678` distinct / `4,518` generated states in `_v2_scale.cfg`, `18/18` actions covered with `0` dead actions across all 8 cfgs).
