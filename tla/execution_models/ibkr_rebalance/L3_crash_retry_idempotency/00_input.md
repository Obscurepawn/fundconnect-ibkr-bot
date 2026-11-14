# 00_input — L3_CrashRetryIdempotency  (FROZEN once TLC runs)

## Target & scope
- System / component: Multi-run crash recovery, systemd/CLI retry execution, `ExecutionState` (`completed_intents`) persistence, `apply_cash_cap_to_buys` intent identity preservation, and cross-`clientId` TWS open-order deduplication.
- Source files & lines in scope:
  - `src/fundconnect_bot/cli.py:378-430, 481-520` (`_cmd_execute` & CLI flags `--client-id`, `--bootstrap-fx-usd`, `--allow-fx-convert`, `--state-dir`)
  - `src/fundconnect_bot/execution/broker.py:336-369` (`IbAsyncBrokerAdapter.get_open_orders`: `ib.reqOpenOrders()` client-scoped open-order query vs `ib.reqAllOpenOrders()`)
  - `src/fundconnect_bot/execution/policy.py:139-168` (`ExecutionState`, `save_execution_state`, `load_execution_state`)
  - `src/fundconnect_bot/execution/policy.py:316-323, 442-450, 533-561` (`working_by_sym_side`, `gross_qty - working_qty` offset, and `if intent in completed_intents: continue` skip gate)
  - `src/fundconnect_bot/execution/policy.py:990-1008` (`apply_cash_cap_to_buys`: `replace(o, quantity=scaled_qty)` preserving unscaled `o.intent_id`)
  - `src/fundconnect_bot/execution/policy.py:1074-1084, 1116-1155, 1202-1230` (`execute_market_rebalance`: two-phase state checkpoints and unrecorded `convert_usd_to_hkd` call)
- Imports from prior level:
  - `L1_TwsWalkLimitAndRaces`: `_execute_orders_with_walk` can return `"FILLED"`, `"PARTIAL"`, or `"WORKING"`, or abort mid-batch via unhandled exception before `save_execution_state`.
  - `L2_TwoPhaseCashSettlement`: partial Phase A sell proceeds cause `apply_cash_cap_to_buys` to down-scale Phase B buy quantities.
- Out of scope (deferred):
  - Multi-week `plan_sha256` rollover (`load_execution_state` at `policy.py:165` already isolates distinct `plan_sha256` hashes).

## Properties to check
- `NoDoubleOrderAcrossClientIds` (safety, developer-expectation): Across crash/failover retries (`clientId = 26` -> `clientId = 27` or manual TWS `clientId = 0`), cumulative filled shares plus open working shares across **all** `clientId`s never exceeds planned sell or buy targets (`(InitSellQty - pos_sell) + TotalWorkingSell <= InitSellQty` and `pos_buy + TotalWorkingBuy <= TargetBuyQty`).
- `NoStrandedUnfilledTargetOnRetry` (safety, developer-expectation): If Run 1 leaves a Phase A or Phase B order in non-terminal `"WORKING"` or `"PARTIAL"` status and TWS/exchange subsequently cancels/expires that order before Run 2, re-running `execute` (`RUN2_DONE`) does not strand the portfolio short of target when cash and quotes are valid and `0` working shares remain.
- `NoScaledBuyPermanentUnderfill` (safety, developer-expectation): If Phase A sells partially fill in Run 1 causing `apply_cash_cap_to_buys` to down-scale a Phase B buy (`scaled_qty < TargetBuyQty`) that fills in Run 1, and the remaining Phase A sells finish filling before Run 2, re-running `execute` (`RUN2_DONE`) purchases the remaining target shares rather than permanently underfilling at `scaled_qty`.
- `NoDuplicateFxConversionOnRetry` (safety, developer-expectation): Re-running `fundconnect-bot execute --market US --bootstrap-fx-usd B --allow-fx-convert --live` for the same execution session after an interruption never converts more than `B` cumulative USD (`fx_converted_total <= BootstrapFxUsd`).

## Environment / fault model
- Process crash / unhandled exception (`CrashBeforePhaseASave`, `CrashBeforePhaseBSave`) before either `save_execution_state` checkpoint (`policy.py:1128, 1220`).
- Operator or systemd retry (`StartRetryRun2`) reconnecting with either the same `clientId` (`26`) or a failover `clientId` (`27`).
- Inter-run TWS venue events (`BETWEEN_RUNS`): working orders from Run 1 may fill (`InterRunTwsFillWorking`), cancel/expire (`InterRunTwsCancelWorking`: `ApiCancelled` / `Inactive` / `DAY` close), or remain open (`Working`) when Run 2 starts.
