# 06_notes — L2_TwoPhaseCashSettlement

## Candidate Bug Analysis & Suggested Minimal Fixes (for Phase 2 review)
1. **Candidate L2-A (`broker.py:514-526` vs `broker.py:397`)**:
   - Keep `ib_mod.Forex("USDHKD")` (default `exchange="IDEALPRO"`) in `get_usdhkd_rate` (`broker.py:397`) for market-data quotes, **but** in `convert_usd_to_hkd` (`broker.py:514`) construct `fx_contract = ib_mod.Forex("USDHKD", exchange="FXCONV")` so TWS performs a physical currency conversion without creating a Virtual FX Position!
   - Also check `trade.orderStatus.status` / `filled` after `ib.sleep(1.0)` in `convert_usd_to_hkd` (`broker.py:527-537`) and in `policy.py:1185`: do not deduct `fx_report.sell_amount` from `eff_cash` if `fx_report.status in ("Inactive", "Cancelled", "ApiCancelled")`!
2. **Candidate L2-B1 (`policy.py:954-966`)**:
   - Notice `policy.py:1180-1183` ALREADY computes `eff_settled = min(eff_total, max(after_sells_c.settled_cash, pre_c.settled_cash + realized_sell_net))` specifically so same-session `T+1` US sell proceeds (`realized_sell_net`) ARE included in `eff_settled` while pre-existing unsettled `T+2` FX USD stays excluded!
   - Changing `compute_spendable_cash` (`policy.py:961-965`) to enforce `settled_cash` whenever `config.require_settled_hkd` (or adding `require_settled_cash: bool = True` / enforcing `min(cash_snap.total_cash, cash_snap.settled_cash, cash_snap.available_funds)` for BOTH `"HKD"` and `"USD"` when `require_settled_hkd` / zero-margin guard is enabled) preserves 100% of same-session `SGOV.US -> US stocks` `T+1 == T+1` netting while blocking unsettled `T+2` FX USD from funding `T+1` US buys!
3. **Candidate L2-B2 (`policy.py:1145-1155, 1177-1194`)**:
   - Compute `pre_c` + `realized_sell_net` (and check `settled_cash["USD"]` if strictly requiring settled USD, or at minimum cap/gate `fx_convert_usd` against `settled_cash["USD"]` when zero-margin-interest is required: note that even filled Monday `SGOV.US` sells settle `T+1` Tuesday while `FXCONV` `USD.HKD` settles `T+2`, which on Monday itself actually settles `T+1` USD BEFORE `T+2` FX USD debit — wait: `SGOV` sell on Monday credits `SettledCash["USD"]` on `T+1` Tuesday `+100`, whereas `FXCONV` `USD.HKD` sell on Monday debits `SettledCash["USD"]` on `T+2` Wednesday `-30`, so if `SGOV` ACTUALLY FILLED (`realized_sell_net >= bootstrap_fx_usd`), `SettledCash["USD"]` is `+100` on `T+1` and `+70` on `T+2`! It ONLY goes negative (`-30` on `T+2`) when `SGOV.US` did NOT fill (`realized_sell_net + pre_c.settled_cash < bootstrap_fx_usd`)!).
   - What an awesome insight from our TLA+ clearing pipeline!! Let's highlight that explicitly:
     - When `SGOV.US` (`T+1`) fills `+100` on `day = 0`, `pending_settle["USD"][1] = +100` and `FX` (`T+2`) has `pending_settle["USD"][2] = -30`: on `day = 1`, `settled_cash["USD"] = +100 >= 0`, and on `day = 2`, `settled_cash["USD"] = +70 >= 0`!
     - `settled_cash["USD"]` goes negative (`-30` on `day = 2`) **precisely** because `policy.py:1145-1148` runs `convert_usd_to_hkd(bootstrap_fx_usd)` BEFORE computing `eff_settled = min(eff_total, max(after_sells_c.settled_cash, pre_c.settled_cash + realized_sell_net))` (`policy.py:1180-1183`) and without checking `eff_settled >= bootstrap_fx_usd`! Gating `allow_fx_convert` on `eff_settled >= bootstrap_fx_usd` (falling back to reserving/deferring FX with an alert when `eff_settled < bootstrap_fx_usd`) eliminates the margin loan cleanly while still allowing Monday `SGOV.US (filled, T+1) -> FXCONV USD.HKD (T+2)`!
   - Also note `policy.py:1179-1184`: if external outflows lowered `after_sells_c.total_cash` during Phase A, `max(after_sells_c.total_cash, pre_c.total_cash + realized_sell_net)` could overstate `eff_total` if `after_sells_c` already reflects fills plus an external debit; worth noting for `@tla-reviewer`.

---

## Stage-1 Adversarial Review Update (2026-11-15)
- **Verdict**:
  - `L2-A` (`NoUnintendedVirtualFxPosition`): **CONFIRMED** (`@tla-reviewer` `#56`).
  - `L2-B1` (`USD` `settled_cash` in `compute_spendable_cash`): **`retired: refuted` (`out-of-regime / safe-by-design`, `lessons.md` R1.3, R4.2, R4.3)** — `tests/test_fundconnect_bot.py:1002-1016` and `README.md:85-106` establish one-way `USD -> HKD` FX (no `T+2` `HKD -> USD` inflows) and require `CashBalance("USD")` from prior same-day `SGOV.US` fills to remain spendable on crash-retry Run 2. Recorded in `tla/LESSONS.md`.
  - `L2-B2` (`ZeroMarginLoanSettledCash` on unguarded `bootstrap_fx_usd`): **CONFIRMED** (`@tla-reviewer` `#56`) — gate live `convert_usd_to_hkd` on non-borrowed USD (`min(pre_fx_total, pre_fx_avail) >= bootstrap_fx_usd`) so filled `T+1` `SGOV.US` proceeds can fund `T+2` `FXCONV` `USD.HKD` (`SettledCash >= 0` on all days `T+0..T+3`) while `WORKING`/insufficient Phase A sells safely skip FX with `[ZERO_MARGIN_FX_GUARD]`.

## Fix validation (`2026-11-15` — `L2_TwoPhaseCashSettlement_v2`)
- **Disposition**:
  - `L2-A` & `L2-B2`: **retired(fixed, validated by `L2_TwoPhaseCashSettlement_v2`)**.
  - `L2-B1`: **retired: refuted (`R6`)** (`compute_spendable_cash` at `policy.py:954-965` preserved unchanged; `L2_TwoPhaseCashSettlement_usd_settled_cash.cfg` remains on frozen v1 as a domain-bound tripwire for reverse `HKD -> USD` flows).
- **Verified code changes modeled**:
  1. **`L2-A` (`src/fundconnect_bot/execution/broker.py:575`)**: `convert_usd_to_hkd` constructs `fx_contract = ib_mod.Forex("USDHKD", exchange="FXCONV")` (`ExecBootstrapFx`, `ExecBootstrapFxRejected`), keeping `virtual_fx_open = FALSE` while `get_usdhkd_rate` (`broker.py:419`) retains `IDEALPRO` quotes.
  2. **`L2-B2` (`src/fundconnect_bot/execution/policy.py:1204-1225, 1283-1291`)**: Before submitting live FX, `execute_market_rebalance` computes `non_borrowed_usd = min(pre_fx_total, pre_fx_avail)` (`1204-1212`); when Phase A `SGOV.US` stays `WORKING` (`non_borrowed_usd < bootstrap_fx_usd`), `GuardBootstrapFxZeroMargin` (`1213-1219`) skips `convert_usd_to_hkd`, sets `reserve_usd = bootstrap_fx_usd`, and emits `[ZERO_MARGIN_FX_GUARD]`. Rejected FX orders (`INACTIVE`/`CANCELLED`/`APICANCELLED`, `1223-1224`) likewise leave `fx_executed_this_run = False` and set `reserve_usd = bootstrap_fx_usd`.
- **Before / after TLC summary**:
  - Pre-fix (`L2_TwoPhaseCashSettlement.tla`): violated `NoUnintendedVirtualFxPosition` (`6` distinct states, depth `4`) and `ZeroMarginLoanSettledCash` on `MONDAY_BOOTSTRAP_FX` (`15` distinct states, depth `8`).
  - Post-fix (`L2_TwoPhaseCashSettlement_v2.tla`): **PASS** (`0` violations across all 4 invariants; `49` distinct / `55` generated states in `_v2.cfg`, `81` distinct / `91` generated states in `_v2_scale.cfg`, `12/12` actions covered with `0` dead actions).
