# 01_reasoning — L1_TwsWalkLimitAndRaces

## Abstraction choices
- **Single-Order Walk Execution across Both Sides (`BUY` and `SELL`)**: `_execute_orders_with_walk` (`src/fundconnect_bot/execution/policy.py:1020-1046`) processes each `OrderSpec` sequentially with no shared mutable state between loop iterations other than the output `reports` list (unless an unhandled `AssertionError` aborts the entire batch). Modeling a single `OrderSpec` with non-deterministic `side \in Sides` (`{"BUY", "SELL"}`), `ReqQty = 200`, `LotSize = 100`, `ArrivalMidCents = 10000` (`$100.00` US `0.01` tick), and `MaxWalkSteps = 3` exercises every step (`+8 -> +14 -> +20 -> +25 bps` for `BUY`, `-8 -> -14 -> -20 -> -25 bps` for `SELL`), partial fills (`100` of `200`), full fills (`200` of `200`), rejections (`Inactive`), cancels (`ApiCancelled`), `Error 104`, and `WORKING` walk-timeout.
- **Explicit `ib_async` Event-Loop Pump Windows**: Unlike multi-threaded brokers, `ib_async` mutates `Trade.orderStatus` exclusively inside `ib.run(...)` calls (`ib.sleep`, `ib.qualifyContracts`, `ib.reqTickers`, `ib.placeOrder`). By restricting TWS async transitions (`TwsAckOrder`, `TwsPartialFill`, `TwsFullFill`, `TwsRejectInactive`, `TwsAsyncCancel`, `TwsError104WireRace`, `TwsModifyAccepted`) to the exact four blocking calls (`PLACE_SLEEP`, `POLL_SLEEP`, `QUOTE_FETCH`, `MOD_WIRE_SLEEP`), every counterexample trace corresponds to a real `ib_async` single-threaded interleaving rather than a spurious thread race.

## Code -> TLA+ mapping
| Code location | Construct | TLA+ action / operator |
|---|---|---|
| `src/fundconnect_bot/execution/broker.py:410-435` | `IbAsyncBrokerAdapter._trade_to_report` | `TradeToReport` |
| `src/fundconnect_bot/execution/broker.py:437-459` | `IbAsyncBrokerAdapter.place_order` | `BotPlaceOrder`, `BotFinishPlace` |
| `src/fundconnect_bot/execution/broker.py:474-479` | `IbAsyncBrokerAdapter.poll_order` | `BotFinishPoll` |
| `src/fundconnect_bot/execution/broker.py:371-393` & `policy.py:233-278, 1036-1041` | `get_quotes` + `compute_walked_limit_price` | `ComputeWalkedLimit`, `BotQuotesAndComputeWalk` |
| `src/fundconnect_bot/execution/broker.py:461-472` & `policy.py:1042-1045` | `modify_order_limit` (`DoneStates` assert vs wire modify) | `BotModifyCrashOnDoneState`, `BotModifyTransmit`, `BotFinishModify` |
| `docs/ibkr_tws_api_semantics.md:20-51` | TWS `orderStatus` / `execDetails` / `error` callbacks | `TwsAckOrder`, `TwsRejectInactive`, `TwsPartialFill`, `TwsFullFill`, `TwsAsyncCancel`, `TwsModifyAccepted`, `TwsError104WireRace` |

## Expected state space & bounds
- `Sides = {"BUY", "SELL"}`, `ArrivalMidCents = 10000`, `ReqQty = 200`, `LotSize = 100`, `MaxWalkSteps = 3`, `LiveQuoteSet = {"TIGHT_MOVE", "WIDE_SPREAD", "HALTED"}`.
- Estimated distinct states: ~3,000–8,000 states (`< 3s` in TLC), with 100% action coverage in `L1_TwsWalkLimitAndRaces_base.cfg`.

## Uncertainties / questions for code check
- None (`0` UNCLEAR/TODO): `ib_async`'s `assert trade.orderStatus.status not in OrderStatus.DoneStates` inside `IB.placeOrder` and TWS `Error 104` / `110` / `201` semantics are documented in `docs/ibkr_tws_api_semantics.md:33-51` and verified directly against `broker.py:371-480` and `policy.py:1011-1047`.
