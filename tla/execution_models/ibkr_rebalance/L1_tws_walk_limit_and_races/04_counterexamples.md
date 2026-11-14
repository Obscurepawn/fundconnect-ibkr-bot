# 04_counterexamples — L1_TwsWalkLimitAndRaces  (FROZEN at close)

## Counterexample 1 — Candidate L1-A: `NoModifyOnTerminalOrder` (`AssertionError` on `DoneStates` modify after `get_quotes`)
- **Property violated**: `NoModifyOnTerminalOrder`
- **Config & invocation**: `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_modify_race.cfg --det` (74 states generated, 47 distinct, depth 7)
- **Classification**: `candidate` (`ib_async` `IB.placeOrder` asserts `trade.orderStatus.status not in OrderStatus.DoneStates`; `broker.get_quotes` pumps the asyncio loop right before `broker.modify_order_limit` calls `ib.placeOrder` without checking `DoneStates`)

### Deterministic TLC Trace (7 states)
1. **State 1 (`Init`)**: `pc = "IDLE"`, `side = "BUY"`, `cur_spec_lmt = 10008` (`$100.08`, `+8 bps` over `$100.00` `arrival_mid`), `tws_status = "NONE"`, `tws_filled = 0`.
2. **State 2 (`BotPlaceOrder`, `src/fundconnect_bot/execution/broker.py:454-456`)**: `ib.placeOrder` transmits `orderId = 1` at `10008` (`PendingSubmit`) and enters `ib.sleep(0.5)` (`pc = "PLACE_SLEEP"`).
3. **State 3 (`BotFinishPlace`, `src/fundconnect_bot/execution/broker.py:457-459`, `policy.py:1021-1027`)**: `place_order` returns `rep_snapshot = [status |-> "WORKING", filled_qty |-> 0, final_limit_cents |-> 10008, reprice_count |-> 0]`; walk step `1` enters `poll_order` (`pc = "POLL_SLEEP"`).
4. **State 4 (`BotFinishPoll`, `src/fundconnect_bot/execution/broker.py:474-479`, `policy.py:1033-1036`)**: `poll_order` still sees `"PendingSubmit"` -> `"WORKING"`, so `_execute_orders_with_walk` proceeds to `broker.get_quotes([cur_spec.symbol])` (`pc = "QUOTE_FETCH"`).
5. **State 5 (`TwsRejectInactive`, `src/fundconnect_bot/execution/broker.py:376-377, 418`)**: Inside `get_quotes()`, `ib.qualifyContracts` / `ib.reqTickers` pumps the `ib_async` socket loop; an async TWS callback (`Error 110`/`201` -> `tws_status = "Inactive"`, or equivalently `execDetails` + `orderStatus` -> `tws_status = "Filled"` / `"ApiCancelled"`) transitions `trade.orderStatus.status` into `OrderStatus.DoneStates`.
6. **State 6 (`BotQuotesAndComputeWalk`, `src/fundconnect_bot/execution/policy.py:1036-1042`)**: `compute_walked_limit_price` computes `pending_lmt = 10014` (`+14 bps`) and calls `broker.modify_order_limit("1", cur_spec, Decimal("100.14"))` (`pc = "CALL_MODIFY"`).
7. **State 7 (`BotModifyCrashOnDoneState`, `src/fundconnect_bot/execution/broker.py:465-468`)**: `modify_order_limit` mutates `trade.order.lmtPrice = 100.14` (`broker.py:467`) and invokes `ib.placeOrder(trade.contract, trade.order)` (`broker.py:468`) while `tws_status = "Inactive" \in DoneStates`, hitting `ib_async`'s `AssertionError` (`pc = "CRASHED_ASSERT_DONE"`) and aborting `execute_market_rebalance` mid-batch before `save_execution_state` (`policy.py:1128, 1220`).

---

## Counterexample 2 — Candidate L1-B: `FillAccountingExact` (Stale `FillReport` returned when `compute_walked_limit_price` returns `None`)
- **Property violated**: `FillAccountingExact`
- **Config & invocation**: `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_stale_report.cfg --det` (48 states generated, 35 distinct, depth 6)
- **Classification**: `candidate` (`_execute_orders_with_walk` does not re-read `trade.orderStatus` when `new_lmt is None` after `broker.get_quotes()` pumps `ib_async`)

### Deterministic TLC Trace (6 states)
1. **States 1–4 (`Init` -> `BotPlaceOrder` -> `BotFinishPlace` -> `BotFinishPoll`)**: `orderId = 1` placed at `10008`; `poll_order` (`policy.py:1033`) records `rep_snapshot = [status |-> "WORKING", filled_qty |-> 0, final_limit_cents |-> 10008, reprice_count |-> 0]` and enters `broker.get_quotes()` (`pc = "QUOTE_FETCH"`).
2. **State 5 (`TwsRejectInactive`, `src/fundconnect_bot/execution/broker.py:376-377`)**: During `get_quotes()`, `ib_async` processes an async TWS status update moving `tws_status` to `"Inactive"` (or `"Filled"` with `tws_filled = 200`).
3. **State 6 (`BotQuotesAndComputeWalk`, `src/fundconnect_bot/execution/policy.py:1036-1041, 1046`)**: `get_quotes()` returns `"WIDE_SPREAD"` (`90 bps > max_spread_bps` or already at `25 bps` cap), so `compute_walked_limit_price` returns `None` (`0`). `policy.py:1040-1041` executes `if new_lmt is None: break` and `policy.py:1046` appends the **stale** `rep_snapshot` (`status = "WORKING", filled_qty = 0`), violating `FillAccountingExact` (`tws_status = "Inactive"` misreported as `"WORKING"`, or `tws_filled = 200` undercounted as `0` in `_realized_sell_net_proceeds` at `policy.py:938-950`).

---

## Counterexample 3 — Candidate L1-C: `FillAccountingExact` (`Error 104` wire race overwriting `final_limit_price` & `reprice_count`)
- **Property violated**: `FillAccountingExact`
- **Config & invocation**: `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_error104.cfg --det` (102 states generated, 67 distinct, depth 9)
- **Classification**: `candidate` (`modify_order_limit` mutates `trade.order.lmtPrice` in place before `ib.placeOrder` and never restores it or suppresses `cnt = prev_cnt + 1` when TWS rejects the modify with `Error 104` because the order already filled on the venue)

### Deterministic TLC Trace (9 states)
1. **States 1–6 (`Init` -> `BotPlaceOrder` -> `BotFinishPlace` -> `BotFinishPoll` -> `TwsAckOrder` -> `BotQuotesAndComputeWalk`)**: `orderId = 1` is active (`tws_status = "PreSubmitted"`) at `venue_lmt_px = 10008`; walk step `1` computes `pending_lmt = 10014` (`pc = "CALL_MODIFY"`).
2. **State 7 (`BotModifyTransmit`, `src/fundconnect_bot/execution/broker.py:467-469`)**: `modify_order_limit` overwrites `trade.order.lmtPrice = 100.14` (`trade_lmt_px = 10014`), calls `ib.placeOrder`, and sleeps `0.5s` (`pc = "MOD_WIRE_SLEEP"`).
3. **State 8 (`TwsError104WireRace`, `src/fundconnect_bot/execution/broker.py:468-469`)**: On the venue, the order had already matched in full (`tws_filled = 200`, `tws_status = "Filled"`) at `venue_lmt_px = 10008`; TWS emits `Error 104` ("Can't modify a filled order") and rejects the `10014` modification.
4. **State 9 (`BotFinishModify`, `src/fundconnect_bot/execution/broker.py:470-472`, `policy.py:1042-1046`)**: `modify_order_limit` increments `reprice_cnt = 1` and builds `FillReport` from the mutated `trade.order.lmtPrice`, returning `final_report = [status |-> "FILLED", filled_qty |-> 200, final_limit_cents |-> 10014, reprice_count |-> 1]` even though the venue never accepted `10014` (`venue_lmt_px = 10008`).

---

## Stage-1 Adversarial Review Dispositions (2026-11-15)
- **Candidate L1-A (`NoModifyOnTerminalOrder`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): async DoneStates callback during broker.get_quotes() triggers unhandled ib_async AssertionError in modify_order_limit -> ib.placeOrder (src/fundconnect_bot/execution/broker.py:461-472, policy.py:1036-1042).`
- **Candidate L1-B (`FillAccountingExact` — stale `FillReport` on `new_lmt is None`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): _execute_orders_with_walk breaks without refreshing rep after get_quotes() when compute_walked_limit_price returns None (src/fundconnect_bot/execution/policy.py:1036-1046).`
- **Candidate L1-C (`FillAccountingExact` — `Error 104` `lmtPrice` mutation)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): modify_order_limit mutates trade.order.lmtPrice before ib.placeOrder and increments reprice_count even when TWS rejects the modify on the wire with Error 104 (src/fundconnect_bot/execution/broker.py:465-472).`
