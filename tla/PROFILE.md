# PROFILE — `fundconnect-ibkr-bot`

## Mission
Mechanically verify the gap-protected sizing, HKEX microstructure rounding, IBKR TWS / `ib_async` walk-limit execution, multi-currency `T+1`/`T+2` zero-margin-interest cash settlement, and crash/retry idempotency of `fundconnect-ibkr-bot` (`src/fundconnect_bot/`).

## Source roots & exclusions
- **Primary modeled source roots** (relative to `<repo-root>` = `/home/obscure/fundconnect-ibkr-bot`):
  - `src/fundconnect_bot/execution/policy.py` — 6-tier gap/shock state machine, defensive fallback redirection, two-phase sell-before-buy batch builder, walk-limit driver, zero-margin spendable cash & buy cash-cap scaler, and `ExecutionState` JSON persistence.
  - `src/fundconnect_bot/execution/broker.py` — `IbAsyncBrokerAdapter` (`ib_async` live socket wrapper for positions, `accountValues()` cash tags, `reqOpenOrders()` open orders, quotes, `placeOrder` placement/modification, and `USD.HKD` FX conversion) and `MockBrokerAdapter`.
  - `src/fundconnect_bot/execution/hkex_rules.py` — HKEX Second Schedule Spread Table (`Part A`, `Part B` `3053.HK`, `Part D` equity/gold ETFs), US Reg NMS `$0.01` tick rounding, `floor_to_lot`, `split_qty_into_lot_slices`, and SEHK leading-zero symbol stripping (`"0003.HK" <-> ("3", "SEHK", "HKD")`).
  - `src/fundconnect_bot/cli.py` — `fundconnect-bot` CLI entry points (`plan`, `simulate-monday`, `execute`) and session-date / live / FX CLI flags.
  - `src/fundconnect_bot/config.py` — `ExecutionPolicyConfig`, `IbkrConnectionConfig`, `SleeveWeights`, and US/HKEX board-lot universes.
  - `docs/ibkr_tws_api_semantics.md` — Verified IBKR TWS API & `ib_async` wire/wrapper semantics reference.
- **Exclusions**:
  - `.venv/`, `__pycache__/`, `data/`, `plans/`, `state/`, `logs/`, `tla/tools/`.
  - Strategy alpha signal math (`src/fundconnect_bot/strategies/{us_macro,us_quality_mom,hk_etf,hk_bluechip}.py`) is upstream of `WeeklyPlan` (`composite.py`) and treated as immutable signed inputs (`plan_sha256`) to execution.

## Build & runtime model (concurrency/dispatch topology — `lessons.md` R2/R5)
- **Language & runtime**: Python 3.12 standalone CLI (`fundconnect-bot`), invoked as oneshot processes via `systemd --user` timers (`deploy/fundconnect-{plan,hk,us}.service`) or manual CLI commands.
- **Intra-process concurrency (`ib_async` + TWS socket)**:
  - Each `fundconnect-bot execute` invocation runs on a single OS thread driving `ib_async.IB()`, which wraps a cooperative `asyncio` event loop connected over TCP to IBKR TWS / IB Gateway (`clientId = 26` by default, `config.py:256`).
  - Python code does not spawn worker threads; however, **every synchronous `ib_async` call** — `ib.sleep()`, `ib.qualifyContracts()`, `ib.reqTickers()`, `ib.reqOpenOrders()`, `ib.placeOrder()` — pumps the `asyncio` socket loop and mutates `Trade.orderStatus` in place as `orderStatus`, `execDetails`, and `error` frames stream asynchronously from TWS.
  - **External concurrency**: IBKR TWS / Gateway and the exchange matching engines (`SMART`, `SEHK`, `IDEALPRO`/`FXCONV`) execute concurrently with the Python process. Orders transition asynchronously through `OrderStatus.ActiveStates` (`ApiPending`, `PendingSubmit`, `PreSubmitted`, `Submitted`) and `PendingCancel` into `OrderStatus.DoneStates` (`Filled`, `Cancelled`, `ApiCancelled`, `Inactive`).
- **Inter-process / crash-recovery concurrency**:
  - Sequential or retrying CLI invocations (Mon–Wed `--enforce-session-date` timers, crash restarts, or failover `--client-id` runs) plus manual TWS desktop orders (`clientId = 0`) share the same IBKR account state (`portfolio()`, `accountValues()`, open orders on TWS) and the local JSON state file `state/execution_state_<YYYYMMDD>_<US|HK>.json`.

## Subsystem map
1. **`execution / ibkr_rebalance` (`tla/execution_models/ibkr_rebalance/`)** — 4-level verification ladder:
   - `L0_gap_sizing_and_hkex`: 6-tier gap sizing, defensive redirection conservation, HKEX Spread Table tick rounding, board-lot slicing, and deterministic Phase A -> Phase B ordering.
   - `L1_tws_walk_limit_and_races`: `IbAsyncBrokerAdapter` + `_execute_orders_with_walk` interacting with async TWS `OrderStatus` transitions, `get_quotes()` event-loop callbacks, `DoneStates` `AssertionError` / `Error 104` modify races, and `Error 110`/`201` -> `Inactive`.
   - `L2_two_phase_cash_settlement`: Two-phase cash accounting, `USD` (`T+1`) vs `HKD` (`T+2` SEHK, `T+2` FX) `CashBalance` / `SettledCash` / `AvailableFunds`, `IDEALPRO` vs `FXCONV` Virtual FX Positions, and zero-margin-loan guarantees.
   - `L3_crash_retry_idempotency`: Crash/retry persistence via `ExecutionState.completed_intents`, cross-`clientId` `reqOpenOrders()` vs `reqAllOpenOrders()`, cancelled `WORKING`/`PARTIAL` intents, cash-cap-scaled `intent_id` reuse, and `--allow-fx-convert` retry idempotency.

## Fault model
- **Design-tolerated faults & asynchronous events**:
  - Halted, missing, or crossed Level-1 quotes (`bid <= 0`, `ask <= 0`, `ask < bid`) and wide bid-ask spreads (`> 35 bps` US, `> 60 bps` HK) (`policy.py:626-646, 520-524`).
  - Partial fills, delayed fills (`WORKING` across `max_walk_steps = 3`), venue/TWS rejections (`Error 110` / `Error 201` -> `Inactive`), async cancellations (`Cancelled`, `ApiCancelled`), and fill-vs-modify wire races (`Error 104`).
  - Process crash/interruption between order placement and `save_execution_state`, subsequent CLI retries (`--state-dir state`), reconnects under alternate `clientId`s, and manual TWS GUI orders (`clientId = 0`).
- **Out-of-scope / non-tolerated faults**:
  - Byzantine broker corruption (TWS forging arbitrary invalid execution reports violating exchange rules) or local filesystem corruption mid-atomic file write.

## Model-size defaults (discovered from `src/fundconnect_bot/config.py`)
- `max_walk_steps = 3`, `initial_limit_bps = 8`, `walk_step_bps = 6`, `max_slippage_bps = 25` (`config.py:236-239`).
- `severe_sell_slices = 3` (`config.py:229`); gap thresholds `-12%`, `-5%`, `-3%`, `+3%`, `+6%`, `moderate_stage_ratio = 0.50` (`config.py:221-228`).
- Spread caps: `max_spread_bps_us = 35.0`, `max_spread_bps_hk = 60.0` (`config.py:232-233`).
- Fee buffers: `us_fee_buffer_bps = 15.0`, `hk_stock_fee_buffer_bps = 25.0`, `hk_etf_fee_buffer_bps = 12.0`, `require_settled_hkd = True` (`config.py:243-246`).
- Default connection `client_id = 26` (`config.py:256`).
- Explicit TLC bounds at `VIBE_TLA_EFFORT=high`: 2–3 representative symbols (non-defensive + defensive fallback), board lots `{1, 100, 500}`, walk steps `0..3`, clearing days `T+0..T+2`, 2 CLI runs across 2 `clientId`s (`{0, 26, 27}`).

## Toolchain
- Java: OpenJDK `21.0.11` (`java -version`).
- TLC: `tla2tools.jar` `v1.8.0` at `/home/obscure/fundconnect-ibkr-bot/tla/tools/tla2tools.jar` (recorded in `tla/config.env`, `VIBE_TLA_EFFORT=high`).
