# 06_notes — L1_TwsWalkLimitAndRaces

## Candidate Bug Analysis & Suggested Minimal Fixes (for Phase 2 review)
1. **Candidate L1-A (`broker.py:461-472`) & Candidate L1-C (`broker.py:465-472`)**:
   - In `IbAsyncBrokerAdapter.modify_order_limit`, check `if str(trade.orderStatus.status or "") in ("Filled", "Cancelled", "ApiCancelled", "Inactive") or round(float(trade.orderStatus.filled or 0)) >= spec.quantity:` **before** mutating `trade.order.lmtPrice` or calling `ib.placeOrder`, returning `self._trade_to_report(order_id, trade, prev_spec, prev_cnt)` immediately without touching `trade.order.lmtPrice` or incrementing `prev_cnt`.
   - Additionally, snapshot `prev_lmt = trade.order.lmtPrice` before `ib.placeOrder` and observe TWS `errorEvent` (`errorCode == 104` for `reqId == trade.order.orderId`, or `trade.log` entries with `errorCode == 104`): if `Error 104` rejected the modify while `trade` filled at the old limit, restore `trade.order.lmtPrice = prev_lmt` and keep `cnt = prev_cnt` so `FillReport.final_limit_price` and `reprice_count` stay exact.
2. **Candidate L1-B (`policy.py:1036-1046`)**:
   - In `_execute_orders_with_walk`, after `q_map = broker.get_quotes([cur_spec.symbol])` (`policy.py:1036`), refresh `rep = broker.poll_order(rep.order_id, wait_sec=0.0)` and check `if rep.status in ("FILLED", "CANCELLED"): break` **before** evaluating `if new_lmt is None: break` (or refresh `rep = broker.poll_order(rep.order_id, wait_sec=0.0)` whenever `new_lmt is None`), ensuring `reports.append(rep)` always reflects callbacks processed during `get_quotes()`.
3. **`WORKING` walk-step timeout observation (`policy.py:1027-1046`, `broker.py:481-487`)**:
   - When `max_walk_steps` expires with `tws_status = "Submitted"`, `_execute_orders_with_walk` does not call `broker.cancel_order(rep.order_id)` and instead returns `status = "WORKING"` (or `"PARTIAL"`). Leaving a bounded `DAY` limit order working in the book during the session is intentional in a walk-limit design, **provided** `completed_intents` does not permanently treat `"WORKING"` / `"PARTIAL"` as terminal completion if TWS later cancels the order (modeled and checked explicitly in `L3_crash_retry_idempotency`).

---

## Stage-1 Adversarial Review Update (2026-11-15)
- **Verdict**: `L1-A`, `L1-B`, and `L1-C` all **CONFIRMED** (`@tla-reviewer` `#56`).
- **Additional Reviewer-Caught Gap `M1` (`confirmed`)**: On coarse-tick HKEX instruments where `1` Spread Table tick exceeds `walk_step_bps` (`6.0 bps`, e.g., `3988.HK` @ `6.28 HKD` where `0.01 = 15.9 bps > 6.0 bps`), step 1 `+14 bps` quantizes back down to `cur_spec.limit_price`, causing `compute_walked_limit_price` (`src/fundconnect_bot/execution/policy.py:268, 277`) to return `None` and `_execute_orders_with_walk` (`policy.py:1040-1041`) to `break` prematurely instead of advancing to step 2/3 (`+20/+25 bps`) where the next tick IS crossed. `L1_TwsWalkLimitAndRaces_v2.tla` will explicitly model coarse-tick quantization alongside `1 bps` ticks to verify `M1` (`CoarseTickWalkReachesCap`) in addition to `L1-A`, `L1-B`, and `L1-C`.

## Fix validation (`2026-11-15` — `L1_TwsWalkLimitAndRaces_v2`)
- **Disposition**: **retired(fixed, validated by `L1_TwsWalkLimitAndRaces_v2`)** across `L1-A`, `L1-B`, `L1-C`, and `M1`.
- **Verified code changes modeled**:
  1. **`L1-A` (`src/fundconnect_bot/execution/broker.py:486-492`)**: `modify_order_limit` checks `ib_status in ("Filled", "Cancelled", "ApiCancelled", "Inactive") or filled_now >= spec.quantity` before mutating `trade.order.lmtPrice` or calling `ib.placeOrder`, returning `_trade_to_report(order_id, trade, prev_spec, prev_cnt)` cleanly (`BotModifyShortCircuitDone`).
  2. **`L1-B` (`src/fundconnect_bot/execution/policy.py:1045-1048`)**: `_execute_orders_with_walk` calls `rep = broker.poll_order(rep.order_id, wait_sec=0.0)` and `if rep.status in ("FILLED", "CANCELLED"): break` immediately after `broker.get_quotes([cur_spec.symbol])` (`BotPostQuoteTerminalBreak`).
  3. **`L1-C` (`src/fundconnect_bot/execution/broker.py:494-528`)**: `modify_order_limit` snapshots `prev_lmt = trade.order.lmtPrice`, subscribes `_on_error` on `ib.errorEvent` (`error_code == 104`) and inspects new `trade.log` entries, rolling back `trade.order.lmtPrice = prev_lmt` and keeping `(prev_spec, prev_cnt)` on `Error 104` (`BotFinishModifyError104`).
  4. **`M1` (`src/fundconnect_bot/execution/policy.py:1051-1057`)**: When `new_lmt is None` on a coarse HKEX tick (`16 bps > 6 bps walk_step_bps`), `_execute_orders_with_walk` checks `any(compute_walked_limit_price(cur_spec, live_q, later_step, config) is not None for later_step in range(step + 1, config.max_walk_steps + 1))` and `continue`s rather than breaking prematurely (`BotCoarseTickContinue`).
- **Before / after TLC summary**:
  - Pre-fix (`L1_TwsWalkLimitAndRaces.tla`): violated `NoModifyOnTerminalOrder` (`47` distinct states, depth `7`) and `FillAccountingExact` (`35` & `67` distinct states, depths `6` & `9`).
  - Post-fix (`L1_TwsWalkLimitAndRaces_v2.tla`): **PASS** (`0` violations across all 6 invariants including `CoarseTickWalkReachesNextTick`; `660` distinct / `1,250` generated states in `_v2.cfg`, `708` distinct / `1,352` generated states in `_v2_scale.cfg`, `20/20` actions covered with `0` dead actions across all 7 cfgs).
