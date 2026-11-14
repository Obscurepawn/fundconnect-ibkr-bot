# 00_input — L1_TwsWalkLimitAndRaces  (FROZEN once TLC runs)

## Target & scope
- System / component: `IbAsyncBrokerAdapter` order placement, polling, quote refresh, and in-place limit repricing interacting with `_execute_orders_with_walk` and official IBKR TWS / `ib_async` `OrderStatus` transitions.
- Source files & lines in scope:
  - `src/fundconnect_bot/execution/broker.py:371-393` (`IbAsyncBrokerAdapter.get_quotes`: `ib.qualifyContracts` + `ib.reqTickers`, pumping the `ib_async` asyncio event loop)
  - `src/fundconnect_bot/execution/broker.py:410-435` (`IbAsyncBrokerAdapter._trade_to_report`: mapping TWS `orderStatus` to `FILLED | CANCELLED | PARTIAL | WORKING`)
  - `src/fundconnect_bot/execution/broker.py:437-459` (`IbAsyncBrokerAdapter.place_order`: `ib.placeOrder` + `ib.sleep(0.5)`)
  - `src/fundconnect_bot/execution/broker.py:461-472` (`IbAsyncBrokerAdapter.modify_order_limit`: mutating `trade.order.lmtPrice`, reusing `orderId` via `ib.placeOrder`, `ib.sleep(0.5)`)
  - `src/fundconnect_bot/execution/broker.py:474-487` (`IbAsyncBrokerAdapter.poll_order` & `cancel_order`)
  - `src/fundconnect_bot/execution/policy.py:233-278` (`compute_walked_limit_price`: bounded walk steps `+8 -> +14 -> +20 -> +25 bps` capped at `max_slippage_bps = 25.0`)
  - `src/fundconnect_bot/execution/policy.py:1011-1047` (`_execute_orders_with_walk`: place -> poll -> `get_quotes` -> `compute_walked_limit_price` -> `modify_order_limit`)
- Imports from prior level:
  - `HkexTickAndBoardLotValid` (`L0_GapSizingAndHkex`): every `OrderSpec` entering `_execute_orders_with_walk` has positive board-lot-aligned `quantity` and tick-valid initial `limit_price` within `[arrival_mid - 25 bps, arrival_mid + 25 bps]`.
- Out of scope (deferred):
  - Multi-currency `CashBalance` vs `SettledCash` vs `AvailableFunds` and `USD.HKD` FX conversion (`L2_two_phase_cash_settlement`).
  - Cross-run `ExecutionState` (`completed_intents`) persistence and cross-`clientId` `open_orders` (`L3_crash_retry_idempotency`).

## Properties to check
- `LimitNeverExceedsSlippageCeiling` (safety, developer-expectation): Every submitted or modified limit price sent to TWS respects the directional `max_slippage_bps` (`25 bps`) ceiling around `arrival_mid` (`limit_cents <= 10025` for `BUY` at `arrival_mid = 10000`; `limit_cents >= 9975` for `SELL` at `arrival_mid = 10000`).
- `NoDuplicateOrderOnModify` (safety, developer-expectation): Repricing an order during `_execute_orders_with_walk` reuses the existing TWS `orderId` in place (`Cardinality(submitted_order_ids) <= 1`), never transmitting duplicate child orders.
- `NoModifyOnTerminalOrder` (safety, developer-expectation): `modify_order_limit` never invokes `ib.placeOrder` on a `Trade` whose `trade.orderStatus.status` has already entered `OrderStatus.DoneStates` (`Filled`, `Cancelled`, `ApiCancelled`, `Inactive`), avoiding an unhandled `ib_async` `AssertionError` mid-batch.
- `FillAccountingExact` (safety, developer-expectation): Upon completion of `_execute_orders_with_walk` (`pc = "DONE"`), the returned `FillReport` reflects the exact `filled_qty`, terminal vs working status, and venue-accepted limit price of `trade` (no undercounted fills, no dead `Inactive`/`Cancelled` orders misreported as `WORKING`/`PARTIAL`, and no rejected `Error 104` limit price overwriting `final_limit_price`).

## Environment / fault model
- Official TWS / `ib_async` asynchronous state machine (`docs/ibkr_tws_api_semantics.md`):
  - `OrderStatus` lifecycle: `PendingSubmit -> PreSubmitted -> Submitted -> Filled` (with optional intermediate partial fills `0 < filled < totalQuantity`), plus rejection to `Inactive` (`Error 110` tick-size violation or `Error 201` margin/permission rejection) and unsolicited `Cancelled` / `ApiCancelled`.
  - Async callbacks arrive whenever `ib_async` runs its socket loop: during `place_order`'s `ib.sleep(0.5)` (`broker.py:456`), `poll_order`'s `ib.sleep(wait_sec)` (`broker.py:477`), `get_quotes`'s `ib.qualifyContracts` / `ib.reqTickers` (`broker.py:376-377`), and `modify_order_limit`'s `ib.sleep(0.5)` (`broker.py:469`).
  - Wire-level modify/fill race (`Error 104`, "Can't modify a filled order") when `ib.placeOrder` transmits a modify for a `Submitted` order that concurrently fills on the venue before the modify arrives.
  - Live quote transitions between tight spread (`14 bps`), wide spread (`90 bps > max_spread_bps`), and halted (`0/0`), plus full `max_walk_steps` exhaustion leaving a `DAY` order `WORKING`.
