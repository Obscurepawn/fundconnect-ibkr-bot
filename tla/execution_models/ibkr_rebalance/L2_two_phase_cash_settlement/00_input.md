# 00_input — L2_TwoPhaseCashSettlement  (FROZEN once TLC runs)

## Target & scope
- System / component: Two-phase cash accounting, spendable-cash gating, Phase B buy capping, and `USD.HKD` FX conversion across `USD` (`T+1` equities) and `HKD` (`T+2` equities, `T+2` spot FX) settlement cycles.
- Source files & lines in scope:
  - `src/fundconnect_bot/execution/broker.py:280-334` (`IbAsyncBrokerAdapter.get_cash`: reading per-currency `CashBalance`, `TotalCashBalance`, `SettledCash`, and Base-Currency-only `AvailableFunds`)
  - `src/fundconnect_bot/execution/broker.py:488-538` (`IbAsyncBrokerAdapter.convert_usd_to_hkd`: constructing `Forex("USDHKD")` on default `IDEALPRO` and submitting `SELL` `LimitOrder`)
  - `src/fundconnect_bot/execution/policy.py:909-951` (`_unit_cost_with_fee` & `_realized_sell_net_proceeds`)
  - `src/fundconnect_bot/execution/policy.py:954-966` (`compute_spendable_cash`: checking `settled_cash` only when `currency == "HKD" and config.require_settled_hkd`)
  - `src/fundconnect_bot/execution/policy.py:968-1009` (`apply_cash_cap_to_buys`: proportional scaling + board-lot flooring within `spendable_cash`)
  - `src/fundconnect_bot/execution/policy.py:1115-1206` (`execute_market_rebalance`: Phase A sells -> cash refresh -> optional `convert_usd_to_hkd` -> `eff_cash` construction -> `apply_cash_cap_to_buys` -> Phase B buys)
- Imports from prior level:
  - `SellBeforeBuyDeterministicOrder` (`L0_GapSizingAndHkex`) and `FillReport` outcomes `{"FILLED", "WORKING"}` (`L1_TwsWalkLimitAndRaces`).
- Out of scope (deferred):
  - Multi-run crash/restart persistence (`ExecutionState.completed_intents`) and cross-`clientId` open-order deduplication (`L3_crash_retry_idempotency`).

## Properties to check
- `BuyBudgetWithinCap` (safety, developer-expectation): After `apply_cash_cap_to_buys` (`src/fundconnect_bot/execution/policy.py:968-1008`), total Phase B buy outlay (worst-case walk limit + fee buffer) never exceeds the computed `spendable_cash` (`0 <= capped_buy_outlay <= spendable_cash`).
- `NoUnintendedVirtualFxPosition` (safety, developer-expectation): Live `USD.HKD` currency conversion (`src/fundconnect_bot/execution/broker.py:514-526`) routes physical cash conversion via `exchange="FXCONV"` rather than `exchange="IDEALPRO"` (which opens a persistent Virtual FX Position in TWS's FX Portfolio).
- `ZeroMarginLoanSettledCash` (safety, developer-expectation): When unsettled-cash gating is enabled (`require_settled_hkd = True`), `settled_cash[ccy] >= 0` holds across every clearing day `T+0`, `T+1`, `T+2`, `T+3` for both `USD` and `HKD` — i.e., neither Phase B equity buys nor `bootstrap_fx_usd` conversions settle ahead of their funding proceeds to create an overnight negative settled-cash margin loan.

## Environment / fault model
- Official IBKR multi-currency settlement & `accountValues()` semantics (`docs/ibkr_tws_api_semantics.md:53-93`):
  - US equities (`SGOV.US`, US stocks) settle `T+1`; SEHK HK equities settle `T+2`; `USD.HKD` spot FX (`IDEALPRO` / `FXCONV`) settles `T+2`.
  - `CashBalance` (`total_cash`) updates immediately on execution (`settled_cash + sum(unsettled_deltas)`), whereas `SettledCash` updates only on each trade's clearing day (`T+1` or `T+2`).
  - `AvailableFunds` is emitted by TWS in account Base Currency (`USD`, reflecting margin collateral purchasing power) and is `None` on non-base `HKD` (`broker.py:324-328` falls back to `total_cash`).
  - Phase A sells may either fill in-session (`FILLED`) or remain working (`WORKING` after walk timeout from `L1`).
