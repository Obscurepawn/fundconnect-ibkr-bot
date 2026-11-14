# 04_counterexamples — L2_TwoPhaseCashSettlement  (FROZEN at close)

## Counterexample 1 — Candidate L2-A: `NoUnintendedVirtualFxPosition` (`convert_usd_to_hkd` uses `IDEALPRO` instead of `FXCONV`)
- **Property violated**: `NoUnintendedVirtualFxPosition`
- **Config & invocation**: `run_tlc.sh L2_TwoPhaseCashSettlement.tla L2_TwoPhaseCashSettlement_virtual_fx.cfg --det` (6 states generated, 6 distinct, depth 4)
- **Classification**: `candidate` (`IbAsyncBrokerAdapter.convert_usd_to_hkd` constructs `ib_mod.Forex("USDHKD")` with `ib_async`'s default `exchange="IDEALPRO"` rather than `exchange="FXCONV"`, which in IBKR TWS opens a persistent Virtual FX Position in the FX Portfolio rather than executing a pure cash conversion)

### Deterministic TLC Trace (4 states)
1. **State 1 (`Init`)**: `scenario = "MONDAY_BOOTSTRAP_FX"`, `market = "US"`, `bootstrap_fx_req = 30`, `allow_fx_convert = TRUE`, `fx_submitted = FALSE`, `fx_exchange = "NONE"`, `virtual_fx_open = FALSE`.
2. **State 2 (`ExecPhaseASellFilled`, `src/fundconnect_bot/execution/policy.py:1116-1122`)**: Phase A `SGOV.US` sell fills (`realized_sell_net = 100`, `pending_settle["USD"][1] = 100`).
3. **State 3 (`RefreshCashAfterSells`, `src/fundconnect_bot/execution/policy.py:1141`)**: `after_sells_snap = [total |-> 100, settled |-> 0, avail |-> 250]`, advancing to `FX_STEP`.
4. **State 4 (`ExecBootstrapFx`, `src/fundconnect_bot/execution/policy.py:1145-1148`, `src/fundconnect_bot/execution/broker.py:514-526`)**: `convert_usd_to_hkd` submits `Forex("USDHKD")` on `fx_exchange = "IDEALPRO"`, setting `virtual_fx_open = TRUE` and violating `NoUnintendedVirtualFxPosition`.

---

## Counterexample 2 — Candidate L2-B1: `ZeroMarginLoanSettledCash` (`compute_spendable_cash` ignores `settled_cash` for `USD`)
- **Property violated**: `ZeroMarginLoanSettledCash`
- **Config & invocation**: `run_tlc.sh L2_TwoPhaseCashSettlement.tla L2_TwoPhaseCashSettlement_usd_settled_cash.cfg --det` (7 states generated, 7 distinct, depth 7)
- **Classification**: `candidate` (`compute_spendable_cash` at `policy.py:961-965` checks `cash_snap.settled_cash` only when `currency == "HKD" and config.require_settled_hkd`, discarding `eff_cash.settled_cash` for `"USD"` even though `policy.py:1180-1183` explicitly isolated unsettled FX in `eff_settled`)

### Deterministic TLC Trace (7 states)
1. **State 1 (`Init`)**: `scenario = "UNSETTLED_FX_USD"`, `market = "US"`, `settled_cash = [USD |-> 0, HKD |-> 0]`, `pending_settle = [USD |-> <<0, 100>>, HKD |-> <<0, 0>>]` (`+$100k` unsettled `T+2` FX USD -> `pre_cash_snap = [total |-> 100, settled |-> 0, avail |-> 200]`), `phase_b_req = 80`.
2. **State 2 (`ExecPhaseASellUnfilled`, `src/fundconnect_bot/execution/policy.py:1116-1122`)**: No Phase A sells (`realized_sell_net = 0`).
3. **States 3–4 (`RefreshCashAfterSells` -> `SkipOrReserveFx`)**: `after_sells_snap = [total |-> 100, settled |-> 0, avail |-> 200]`, `reserve_usd = 0`.
4. **State 5 (`ComputeSpendableAndCapBuys`, `src/fundconnect_bot/execution/policy.py:961-965, 1180-1204`)**: `eff_snap.settled` is `0`, **but** `compute_spendable_cash` executes `base = min(cash_snap.total_cash, cash_snap.available_funds) = min(100, 200) = 100`, setting `spendable_cash = 100` and `capped_buy_outlay = 80`.
5. **State 6 (`ExecPhaseBBuys`, `src/fundconnect_bot/execution/policy.py:1208-1214`)**: Phase B US equity buys execute for `80` (`T+1` settlement: `pending_settle["USD"] = <<-80, 100>>`).
6. **State 7 (`AdvanceSettlementDay`, `day = 1`)**: On `T+1`, the US equity buy clears (`-80`) while the `T+2` FX USD (`+100`) has not yet settled, driving `settled_cash["USD"] = -80 < 0` (overnight USD margin loan accruing interest!).

---

## Counterexample 3 — Candidate L2-B2: `ZeroMarginLoanSettledCash` (Unguarded `bootstrap_fx_usd` conversion without settled/realized USD check)
- **Property violated**: `ZeroMarginLoanSettledCash`
- **Config & invocation**: `run_tlc.sh L2_TwoPhaseCashSettlement.tla L2_TwoPhaseCashSettlement_fx_unguarded.cfg --det` (15 states generated, 15 distinct, depth 8)
- **Classification**: `candidate` (`execute_market_rebalance` at `policy.py:1145-1148` submits `convert_usd_to_hkd(bootstrap_fx_usd, dry_run=False)` without gating on `settled_cash["USD"]` or even realized Phase A `SGOV.US` fills; if `SGOV.US` stays `WORKING` or has not settled, Margin `AvailableFunds` permits the FX sale and drives `settled_cash["USD"] < 0`)

### Deterministic TLC Trace (8 states)
1. **State 1 (`Init`)**: `scenario = "MONDAY_BOOTSTRAP_FX"`, `market = "US"`, `settled_cash = [USD |-> 0, HKD |-> 0]`, `margin_bonus_usd = 150`, `phase_a_req = 100`, `bootstrap_fx_req = 30`, `allow_fx_convert = TRUE`.
2. **State 2 (`ExecPhaseASellUnfilled`, `src/fundconnect_bot/execution/policy.py:1027-1046, 1116-1122`)**: Phase A `SGOV.US` limit order remains `WORKING` after walk-step timeout (`realized_sell_net = 0`).
3. **State 3 (`RefreshCashAfterSells`, `src/fundconnect_bot/execution/policy.py:1141`)**: `after_sells_snap = [total |-> 0, settled |-> 0, avail |-> 150]`.
4. **State 4 (`ExecBootstrapFx`, `src/fundconnect_bot/execution/policy.py:1145-1148`, `broker.py:514-527`)**: Despite `total = 0` and `settled = 0`, `execute_market_rebalance` calls `convert_usd_to_hkd(30, dry_run=False)`, selling `30` USD against Margin `AvailableFunds` (`pending_settle = [USD |-> <<0, -30>>, HKD |-> <<0, 240>>]`).
5. **States 5–6 (`ComputeSpendableAndCapBuys` -> `ExecPhaseBBuys`)**: `spendable_cash = 0` properly blocks Phase B US equity buys (`capped_buy_outlay = 0`), but the `-30` USD FX obligation is already live.
6. **States 7–8 (`AdvanceSettlementDay`, `day = 1` -> `day = 2`)**: On `T+2` (`day = 2`), the FX sale settles with zero `SGOV.US` proceeds, driving `settled_cash = [USD |-> -30, HKD |-> 240]` (`settled_cash["USD"] < 0`).

---

## Stage-1 Adversarial Review Dispositions (2026-11-15)
- **Candidate L2-A (`NoUnintendedVirtualFxPosition`)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): IbAsyncBrokerAdapter.convert_usd_to_hkd constructs ib_mod.Forex("USDHKD") on default IDEALPRO instead of exchange="FXCONV", opening a Virtual FX Position (src/fundconnect_bot/execution/broker.py:514-526).`
- **Candidate L2-B1 (`ZeroMarginLoanSettledCash` — `USD` `settled_cash` in `compute_spendable_cash`)**: `Reviewed: retired: refuted (out-of-regime / safe-by-design per lessons.md R1.3, R4.2, R4.3) — 2026-11-15 (@tla-reviewer #56): (a) tests/test_fundconnect_bot.py:1002-1016 explicitly specifies compute_spendable_cash(CashSnap("USD", 1_000_000.0, 29.16, 1_000_000.0)) == 1_000_000.0; (b) FX in fundconnect-ibkr-bot is strictly one-way USD -> HKD (README.md:85-106) with zero HKD -> USD T+2 FX inflows, so positive CashBalance("USD") > SettledCash("USD") denotes T+1 US equity proceeds settling on the same T+1 day as US buys; and (c) gating USD on SettledCash would break Monday crash-retry Run 2 after Run 1 sold SGOV.US (realized_sell_net == 0 in Run 2 while CashBalance("USD") == $1M, SettledCash("USD") == $29.16). Retain L2_TwoPhaseCashSettlement_usd_settled_cash.cfg on frozen v1 as a domain-bound tripwire.`
- **Candidate L2-B2 (`ZeroMarginLoanSettledCash` — unguarded `bootstrap_fx_usd` conversion)**: `Reviewed: confirmed — 2026-11-15 (@tla-reviewer #56): execute_market_rebalance submits convert_usd_to_hkd(bootstrap_fx_usd) without gating on non-borrowed USD cash (min(pre_fx_total, pre_fx_avail) >= bootstrap_fx_usd), selling USD on margin when Phase A SGOV.US stays WORKING/halted/insufficient (src/fundconnect_bot/execution/policy.py:1145-1155, 1185-1193).`
