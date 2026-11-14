# 05_output — L2_TwoPhaseCashSettlement  (FROZEN at close)

## What this level proved
- **`BuyBudgetWithinCap`**: `apply_cash_cap_to_buys` (`src/fundconnect_bot/execution/policy.py:968-1009`) guarantees `0 <= capped_buy_outlay <= spendable_cash` across every multi-currency settlement scenario and Phase A fill outcome (`PASS`, 52 distinct states in `L2_TwoPhaseCashSettlement_base.cfg`). Furthermore, `HK_UNSETTLED_FX_HKD` confirmed that `require_settled_hkd = True` (`policy.py:961-962`) blocks spending unsettled `T+2` HKD (`spendable_cash = 0`), and `SAME_SESSION_US` confirmed that same-session `T+1` US sells funding `T+1` US buys net to `settled_cash["USD"] >= 0` on every day.
- **`NoUnintendedVirtualFxPosition`**: **VIOLATED (`candidate`)** — 4-state trace (`L2_TwoPhaseCashSettlement_virtual_fx.cfg`).
- **`ZeroMarginLoanSettledCash`**: **VIOLATED (`candidate`)** — 7-state `UNSETTLED_FX_USD` trace (`L2_TwoPhaseCashSettlement_usd_settled_cash.cfg`) and 8-state `MONDAY_BOOTSTRAP_FX` trace (`L2_TwoPhaseCashSettlement_fx_unguarded.cfg`).

## Finding
- **Candidate L2-A (`NoUnintendedVirtualFxPosition`)**: `candidate(IbAsyncBrokerAdapter.convert_usd_to_hkd constructs Forex("USDHKD") with default exchange="IDEALPRO" instead of exchange="FXCONV", opening a Virtual FX Position in TWS's FX Portfolio)` — `src/fundconnect_bot/execution/broker.py:514-526`.
- **Candidate L2-B1 (`ZeroMarginLoanSettledCash` — `USD` `settled_cash` omitted in `compute_spendable_cash`)**: `candidate(compute_spendable_cash checks cash_snap.settled_cash only for HKD, allowing unsettled T+2 FX USD proceeds in CashBalance to fund T+1 US equity buys and drive SettledCash["USD"] < 0 on T+1)` — `src/fundconnect_bot/execution/policy.py:961-965`.
- **Candidate L2-B2 (`ZeroMarginLoanSettledCash` — unguarded `bootstrap_fx_usd` conversion)**: `candidate(execute_market_rebalance invokes convert_usd_to_hkd(bootstrap_fx_usd) without checking settled/realized USD cash, selling USD on margin when Phase A SGOV.US sells stay WORKING or unsettled)` — `src/fundconnect_bot/execution/policy.py:1145-1148`, `src/fundconnect_bot/execution/broker.py:514-537`.

## Fidelity & coverage  (see references/coverage.md)
- Fidelity: 9 Next-disjunct transitions + `Init` modeled, all cited `file:line` (`src/fundconnect_bot/execution/broker.py:280-334, 488-538`, `src/fundconnect_bot/execution/policy.py:909-1009, 1115-1206`); 0 UNCLEAR/TODO; scaled integer currency units (`$1k` units) and multi-day clearing pipeline documented in `01_reasoning.md`.
- Code coverage: modeled 6 of 6 cash/FX/capping functions (`get_cash`, `convert_usd_to_hkd`, `_unit_cost_with_fee`, `_realized_sell_net_proceeds`, `compute_spendable_cash`, `apply_cash_cap_to_buys`, plus `execute_market_rebalance` cash/FX flow).
- Model coverage: TLC `--coverage` (`coverage.sh L2_TwoPhaseCashSettlement.tla L2_TwoPhaseCashSettlement_base.cfg --det`) — all 10 action(s) fired; 0 zero-coverage actions.

## Exports to the next level
- When Phase A sells partially fill (`0 < realized_sell_net < phase_a_req`), `apply_cash_cap_to_buys` (`policy.py:990-1008`) down-scales Phase B `OrderSpec.quantity` via `replace(o, quantity=scaled_qty)` while keeping `o.intent_id` unchanged — exported directly to `L3_crash_retry_idempotency` (`NoScaledBuyPermanentUnderfill`).
