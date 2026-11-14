# ROADMAP — `fundconnect-ibkr-bot`

## Active Ladder: `execution_models / ibkr_rebalance`

| Level | Live Module | Concern | Status | Verdict / Finding |
|---|---|---|---|---|
| `L0_gap_sizing_and_hkex` | `L0_GapSizingAndHkex_v2` | 6-tier gap classification, defensive redirect budget conservation, HKEX Spread Table tick rounding, board-lot sizing, deterministic Phase A -> Phase B order sorting | Closed (`_v2` synced) | `PASS` (112,383 distinct states; 0 violations; 14/14 actions) |
| `L1_tws_walk_limit_and_races` | `L1_TwsWalkLimitAndRaces_v2` | `IbAsyncBrokerAdapter` + `_execute_orders_with_walk` vs async TWS `OrderStatus` transitions, `get_quotes()` socket callback races, `DoneStates` guard, `Error 104` rollback, coarse-tick walk continuation | Closed (`_v2` validated) | `retired(fixed, validated by L1_TwsWalkLimitAndRaces_v2)` (`L1-A`, `L1-B`, `L1-C`, `M1` all resolved; 708 distinct states; 20/20 actions) |
| `L2_two_phase_cash_settlement` | `L2_TwoPhaseCashSettlement_v2` | Two-phase sell-before-buy cash accounting, `USD` (`T+1`) & `HKD` (`T+2` equities / `T+2` FX) `CashBalance` vs `SettledCash` vs `AvailableFunds`, `FXCONV` physical conversion, `ZERO_MARGIN_FX_GUARD` | Closed (`_v2` validated) | `retired(fixed, validated by L2_TwoPhaseCashSettlement_v2)` (`L2-A`, `L2-B2` resolved; `L2-B1` `retired: refuted` per R6; 81 distinct states; 12/12 actions) |
| `L3_crash_retry_idempotency` | `L3_CrashRetryIdempotency_v2` | Crash/retry idempotency across `ExecutionState.completed_intents`, `reqAllOpenOrders()` across `clientId`s, `FILLED`-only persistence, `:cap:` buy intent IDs, and pre-Phase-B FX checkpoint | Closed (`_v2` validated) | `retired(fixed, validated by L3_CrashRetryIdempotency_v2)` (`L3-A`, `L3-B`, `L3-C`, `L3-D` all resolved; 3,678 distinct states; 18/18 actions) |

## Backlog (secondary components)
- `strategies_models / composite_hysteresis` — Weekly plan rank-buffer hysteresis (`LiveSleeveState.from_live_portfolio_weights` in `src/fundconnect_bot/strategies/composite.py:89-160`) and holiday deferral (`src/fundconnect_bot/data/calendars.py`).
