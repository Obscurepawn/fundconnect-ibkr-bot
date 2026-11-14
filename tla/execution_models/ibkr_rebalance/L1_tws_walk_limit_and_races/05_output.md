# 05_output — L1_TwsWalkLimitAndRaces  (FROZEN at close)

## What this level proved
- **`LimitNeverExceedsSlippageCeiling`**: Across both `BUY` and `SELL` orders and all 3 walk steps (`+8 -> +14 -> +20 -> +25 bps`), `compute_walked_limit_price` (`src/fundconnect_bot/execution/policy.py:233-278`) strictly bounds every transmitted limit price within `[arrival_mid - 25 bps, arrival_mid + 25 bps]` (`PASS`, 380 distinct states in `L1_TwsWalkLimitAndRaces_base.cfg`).
- **`NoDuplicateOrderOnModify`**: `modify_order_limit` (`src/fundconnect_bot/execution/broker.py:461-472`) reuses `trade.order` (`orderId = 1`) in place rather than allocating new order IDs (`PASS`, 380 distinct states).
- **`NoModifyOnTerminalOrder`**: **VIOLATED (`candidate`)** — 7-state trace (`L1_TwsWalkLimitAndRaces_modify_race.cfg`).
- **`FillAccountingExact`**: **VIOLATED (`candidate`)** — 6-state stale-report trace (`L1_TwsWalkLimitAndRaces_stale_report.cfg`) and 9-state `Error 104` price-mutation trace (`L1_TwsWalkLimitAndRaces_error104.cfg`).

## Finding
- **Candidate L1-A (`NoModifyOnTerminalOrder`)**: `candidate(async fill/reject/cancel callback arriving during broker.get_quotes() moves trade.orderStatus.status into OrderStatus.DoneStates before broker.modify_order_limit() calls ib.placeOrder without a DoneStates guard, raising AssertionError mid-batch)` — `src/fundconnect_bot/execution/policy.py:1036-1042`, `src/fundconnect_bot/execution/broker.py:461-472`.
- **Candidate L1-B (`FillAccountingExact` — stale `FillReport` on `new_lmt is None`)**: `candidate(async fill/reject/cancel callback arriving during broker.get_quotes() followed by compute_walked_limit_price returning None causes _execute_orders_with_walk to break and return the stale pre-get_quotes() FillReport)` — `src/fundconnect_bot/execution/policy.py:1036-1046`.
- **Candidate L1-C (`FillAccountingExact` — `Error 104` wire race mutating `final_limit_price` & `reprice_count`)**: `candidate(modify_order_limit mutates trade.order.lmtPrice before ib.placeOrder and unconditionally increments reprice_count even when TWS rejects the modify with Error 104 because the order concurrently filled on the venue)` — `src/fundconnect_bot/execution/broker.py:465-472`.

## Fidelity & coverage  (see references/coverage.md)
- Fidelity: 15 Next-disjunct transitions + `Init` modeled, all cited `file:line` (`src/fundconnect_bot/execution/broker.py:371-480`, `src/fundconnect_bot/execution/policy.py:233-278, 1011-1047`); 0 UNCLEAR/TODO; `ib_async` event-loop pump windows documented in `01_reasoning.md`.
- Code coverage: modeled 6 of 6 single-order execution & walk functions (`place_order`, `poll_order`, `get_quotes`, `modify_order_limit`, `_trade_to_report`, `compute_walked_limit_price`, `_execute_orders_with_walk`; `cancel_order` at `broker.py:481` confirmed unreferenced by `_execute_orders_with_walk`, leaving timed-out `DAY` orders `WORKING` for `L3` analysis).
- Model coverage: TLC `--coverage` (`coverage.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_base.cfg --det`) — all 16 action(s) fired; 0 zero-coverage actions.

## Exports to the next level
- `_execute_orders_with_walk` may return `FillReport.status` in `{"FILLED", "PARTIAL", "WORKING", "CANCELLED"}` (including timed-out `"WORKING"` / `"PARTIAL"` orders after `max_walk_steps` exhaustion), which directly parameterizes Phase A proceed realization (`L2`) and `ExecutionState.completed_intents` persistence (`L3`).
