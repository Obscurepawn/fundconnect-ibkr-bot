# TODO — ibkr_rebalance

Actionable digest for this ladder (`execution_models/ibkr_rebalance`). Auto-maintained by
check / advance / review / fix / sync / resume; per-level detail lives in the `L{n}_{slug}/`
folders, findings are canonical in `../../MEMORY.md`. Last updated: 2026-11-15.

## Next action
- All 4 levels of `execution_models/ibkr_rebalance` (`L0`–`L3`) are closed, re-anchored (`*_v2.tla`), and validated with `0` violations and `0` zero-coverage actions. If extending verification beyond execution, start `/vibe-tla check composite_hysteresis` from `../../ROADMAP.md`.

## Open — needs action
(none)

## Planned — not built yet
(none)

## Watch — may have drifted
- Historical v1 specs (`L0_GapSizingAndHkex.tla`, `L1_TwsWalkLimitAndRaces.tla`, `L2_TwoPhaseCashSettlement.tla`, `L3_CrashRetryIdempotency.tla`) and their `00_input.md` / `04_counterexamples.md` / `05_output.md` files are **intentionally frozen** as pre-fix historical evidence; `anchors.lock` and future `/vibe-tla sync` runs re-anchor strictly against the live `*_v2.tla` specs.

## Closed — for reference
| Level | Verdict | Summary |
|---|---|---|
| `L0_gap_sizing_and_hkex` | `PASS` | Synced `L0_GapSizingAndHkex_v2.tla` (`policy.py:1132-1340`); verified `BudgetConservation`, `NoNegativeOrNaNQty`, `HkexTickAndBoardLotValid`, and `SellBeforeBuyDeterministicOrder` across 112,383 distinct states (14/14 actions covered). |
| `L1_tws_walk_limit_and_races` | `retired(fixed, validated by L1_TwsWalkLimitAndRaces_v2)` | Validated fixes for `L1-A` (`broker.py:486-492` `DoneStates` guard), `L1-B` (`policy.py:1045-1048` post-`get_quotes` `poll_order`), `L1-C` (`broker.py:494-528` `Error 104` `lmtPrice` rollback), and `M1` (`policy.py:1051-1057` coarse-tick walk continuation); 6/6 invariants PASS across 708 distinct states (20/20 actions covered). |
| `L2_two_phase_cash_settlement` (`L2-B1`) | `retired: refuted` | Out-of-regime (`lessons.md` R1.3, R4.2, R4.3; `LESSONS.md` R6): FX flow is strictly one-way `USD -> HKD` (`README.md:85-106`) and `tests/test_fundconnect_bot.py:1002-1016` specifies `CashBalance("USD")` from same-day `T+1` `SGOV.US` sales remains spendable across crash-retry runs. |
| `L2_two_phase_cash_settlement` (`L2-A`, `L2-B2`) | `retired(fixed, validated by L2_TwoPhaseCashSettlement_v2)` | Validated fixes for `L2-A` (`broker.py:575` `Forex("USDHKD", exchange="FXCONV")`) and `L2-B2` (`policy.py:1204-1225` `non_borrowed_usd` `[ZERO_MARGIN_FX_GUARD]` + rejected-FX reserve); 4/4 invariants PASS across 81 distinct states (12/12 actions covered). |
| `L3_crash_retry_idempotency` | `retired(fixed, validated by L3_CrashRetryIdempotency_v2)` | Validated fixes for `L3-A` (`broker.py:341-360` `reqAllOpenOrders()` + `CASH` `USD.HKD` open-order visibility), `L3-B` (`policy.py:1140-1153, 1316-1329` `FILLED`-only persistence), `L3-C` (`policy.py:1005-1014, 1303-1304` `:cap:{orig}:{scaled}` buy intent IDs), and `L3-D` (`policy.py:1164-1247` pre-Phase-B FX checkpoint); 5/5 invariants PASS across 3,678 distinct states (18/18 actions covered). |
