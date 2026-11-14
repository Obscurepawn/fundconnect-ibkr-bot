# 05_output — L3_CrashRetryIdempotency  (FROZEN at close)

## What this level proved
- **`TypeOK`**: Verified across all 1,586 distinct multi-run crash/interleaving/retry states (`PASS`, `L3_CrashRetryIdempotency_base.cfg`).
- **`NoDoubleOrderAcrossClientIds`**: **VIOLATED (`candidate`)** — 7-state trace (`L3_CrashRetryIdempotency_client_id.cfg`).
- **`NoStrandedUnfilledTargetOnRetry`**: **VIOLATED (`candidate`)** — 15-state trace (`L3_CrashRetryIdempotency_stranded_working.cfg`).
- **`NoScaledBuyPermanentUnderfill`**: **VIOLATED (`candidate`)** — 15-state trace (`L3_CrashRetryIdempotency_scaled_buy.cfg`).
- **`NoDuplicateFxConversionOnRetry`**: **VIOLATED (`candidate`)** — 12-state trace (`L3_CrashRetryIdempotency_dup_fx.cfg`).

## Finding
- **Candidate L3-A (`NoDoubleOrderAcrossClientIds`)**: `candidate(IbAsyncBrokerAdapter.get_open_orders calls ib.reqOpenOrders() instead of ib.reqAllOpenOrders(), blinding working_by_sym_side to open orders placed by a prior crashed clientId or manual TWS clientId=0 and causing duplicate order submission)` — `src/fundconnect_bot/execution/broker.py:336-369`.
- **Candidate L3-B (`NoStrandedUnfilledTargetOnRetry`)**: `candidate(execute_market_rebalance persists non-terminal WORKING and PARTIAL statuses in ExecutionState.completed_intents, permanently blocking retry execution when TWS/exchange subsequently cancels or expires the working order)` — `src/fundconnect_bot/execution/policy.py:535-537, 1125-1126, 1217-1218`.
- **Candidate L3-C (`NoScaledBuyPermanentUnderfill`)**: `candidate(apply_cash_cap_to_buys down-scales OrderSpec.quantity via replace(o, quantity=scaled_qty) while keeping o.intent_id unchanged; filling the scaled order burns o.intent_id in completed_intents and permanently blocks buying the remaining target shares on retry after Phase A finishes filling)` — `src/fundconnect_bot/execution/policy.py:1005, 1217-1218`.
- **Candidate L3-D (`NoDuplicateFxConversionOnRetry`)**: `candidate(execute_market_rebalance neither checks nor records bootstrap_fx_usd conversion in ExecutionState, submitting a duplicate USD.HKD conversion when execute is retried for the same session)` — `src/fundconnect_bot/execution/policy.py:139-168, 1145-1148`.

## Fidelity & coverage  (see references/coverage.md)
- Fidelity: 17 Next-disjunct transitions + `Init` modeled, all cited `file:line` (`src/fundconnect_bot/cli.py:378-430, 481-520`, `src/fundconnect_bot/execution/broker.py:336-369`, `src/fundconnect_bot/execution/policy.py:139-168, 316-323, 442-561, 990-1008, 1074-1230`); 0 UNCLEAR/TODO; 2-run composition and scaled share units documented in `01_reasoning.md`.
- Code coverage: modeled 6 of 6 state-persistence, open-order deduplication, cash-cap intent, and retry entry-point paths (`_cmd_execute`, `get_open_orders`, `load_execution_state`, `save_execution_state`, `build_execution_batch` working/completed filters, `apply_cash_cap_to_buys` `intent_id` propagation, and `execute_market_rebalance` Phase A / FX / Phase B checkpoints).
- Model coverage: TLC `--coverage` (`coverage.sh L3_CrashRetryIdempotency.tla L3_CrashRetryIdempotency_base.cfg --det`) — all 18 action(s) fired; 0 zero-coverage actions.

## Exports to the next level
- Terminal level of the `ibkr_rebalance` 4-level ladder (`L0` -> `L1` -> `L2` -> `L3`).
