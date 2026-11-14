# PLAN — `execution_models / ibkr_rebalance` (FROZEN at L0)

## Charter
Mechanically verify the end-to-end execution stack of `fundconnect-ibkr-bot` (`src/fundconnect_bot/execution/{hkex_rules,policy,broker}.py` and `src/fundconnect_bot/cli.py`) as a 4-level compositional TLA+ ladder (`L0 -> L3`), grounded in official IBKR TWS API & `ib_async` runtime semantics (`docs/ibkr_tws_api_semantics.md`).

## Verified Source Scope & Exclusions
- **Source root**: `/home/obscure/fundconnect-ibkr-bot`
- **Included modules**:
  - `src/fundconnect_bot/execution/hkex_rules.py` (`1-269`)
  - `src/fundconnect_bot/execution/policy.py` (`1-1240`)
  - `src/fundconnect_bot/execution/broker.py` (`1-813`)
  - `src/fundconnect_bot/cli.py` (`1-531`)
  - `src/fundconnect_bot/config.py` (`1-267`)
- **Excluded trees**: `.venv/`, `data/`, `plans/`, `state/`, `logs/`, `tla/tools/`, upstream alpha generation (`src/fundconnect_bot/strategies/`).

## Verified Runtime Concurrency Model (`lessons.md` R2 / R5)
1. **Intra-process cooperative event loop (`ib_async`)**:
   - Each `fundconnect-bot execute` run is a single-threaded Python process holding one `ib_async.IB()` TCP connection to TWS / IB Gateway (`clientId = 26` default).
   - Python execution is sequential *except* at synchronous `ib_async` socket-pumping points: `ib.sleep()`, `ib.qualifyContracts()`, `ib.reqTickers()`, `ib.reqOpenOrders()`, and `ib.placeOrder()`. During any of those calls, incoming TWS `orderStatus`, `execDetails`, and `error` frames mutate `Trade.orderStatus` asynchronously in memory.
2. **External asynchronous venue & TWS matching**:
   - TWS and exchange order books (`SMART`, `SEHK`, `IDEALPRO`/`FXCONV`) advance concurrently with Python, driving `OrderStatus.ActiveStates` (`ApiPending`, `PendingSubmit`, `PreSubmitted`, `Submitted`) and `PendingCancel` into `OrderStatus.DoneStates` (`Filled`, `Cancelled`, `ApiCancelled`, `Inactive`).
3. **Multi-run / multi-`clientId` shared state**:
   - Crash-restarts, Mon–Wed systemd timer retries (`--enforce-session-date`), failover `--client-id` connections, and manual TWS GUI orders (`clientId = 0`) share IBKR account state (`portfolio()`, `accountValues()`, open TWS orders) and `state/execution_state_<YYYYMMDD>_<US|HK>.json`.

## Compositional Level Plan (`L0 -> L3`)
1. **`L0_gap_sizing_and_hkex` (`L0_GapSizingAndHkex`)** — *Mode: Faithful transcription*.
   - **Concern**: Deterministic single-snapshot gap/news-shock classification, defensive fallback redirection, HKEX Second Schedule Spread Table tick rounding (`Part A`, `Part B` `3053.HK`, `Part D`), board-lot sizing/slicing, and Phase A -> Phase B ordering (`hkex_rules.py` + `policy.py:194-906`).
   - **Invariants**: `BudgetConservation`, `NoNegativeOrNaNQty`, `HkexTickAndBoardLotValid`, `SellBeforeBuyDeterministicOrder`.
2. **`L1_tws_walk_limit_and_races` (`L1_TwsWalkLimitAndRaces`)** — *Mode: Faithful fault/event injection (TWS async callbacks & rejections)*.
   - **Concern**: `IbAsyncBrokerAdapter` (`broker.py:410-487`) + `_execute_orders_with_walk` (`policy.py:233-278, 1011-1047`) interacting with TWS `OrderStatus` lifecycle, async fills/cancels arriving during `get_quotes()` / `ib.sleep()`, `DoneStates` modify race (`ib_async` `AssertionError` & `Error 104`), `Error 110`/`201` -> `Inactive`, and `WORKING` walk-step timeout.
   - **Invariants**: `LimitNeverExceedsSlippageCeiling`, `NoDuplicateOrderOnModify`, `NoModifyOnTerminalOrder`, `FillAccountingExact`.
3. **`L2_two_phase_cash_settlement` (`L2_TwoPhaseCashSettlement`)** — *Mode: Faithful transcription + multi-day settlement ledger*.
   - **Concern**: `execute_market_rebalance` (`policy.py:1098-1239`) + `convert_usd_to_hkd` (`broker.py:488-538`) + `compute_spendable_cash` & `apply_cash_cap_to_buys` (`policy.py:909-1009`) across `USD` (`T+1`) and `HKD` (`T+2` equities, `T+2` FX) `CashBalance` vs `SettledCash` vs `AvailableFunds`, and `IDEALPRO` vs `FXCONV` FX routing.
   - **Invariants**: `ZeroMarginLoanSettledCash`, `BuyBudgetWithinCap`, `NoUnintendedVirtualFxPosition`.
4. **`L3_crash_retry_idempotency` (`L3_CrashRetryIdempotency`)** — *Mode: Faithful fault injection (process crash/interruption, TWS order cancel, multi-run retry, multi-`clientId` open orders)*.
   - **Concern**: Multi-run `cli.py` + `execute_market_rebalance` sharing `ExecutionState.completed_intents` (`policy.py:535-537, 1005, 1124-1126, 1145-1148, 1216-1218`) and `get_open_orders` (`broker.py:336-369`).
   - **Invariants**: `NoDoubleOrderAcrossClientIds`, `NoStrandedUnfilledTargetOnRetry`, `NoScaledBuyPermanentUnderfill`, `NoDuplicateFxConversionOnRetry`.
