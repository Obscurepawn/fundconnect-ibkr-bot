# MEMORY — `fundconnect-ibkr-bot`

Running verification index for `/home/obscure/fundconnect-ibkr-bot` (`$WORKSPACE = tla/`).

## execution_models / ibkr_rebalance / L0_gap_sizing_and_hkex — PASS (2026-11-16)
- Mode: transcription
- Spec: `execution_models/ibkr_rebalance/L0_gap_sizing_and_hkex/02_models/L0_GapSizingAndHkex_v2.tla` (v1 frozen at `L0_GapSizingAndHkex.tla`)
- Properties checked: `TypeOK`, `BudgetConservation`, `NoNegativeOrNaNQty`, `HkexTickAndBoardLotValid`, `SellBeforeBuyDeterministicOrder` (14,047 distinct states in `_v2.cfg`; 112,383 distinct states in `_v2_scale.cfg`; 14/14 actions covered)
- Finding: none
- Exports: `HkexTickAndBoardLotValid`, `SellBeforeBuyDeterministicOrder`, `BudgetConservation`
- Last synced: 2026-11-16 (`anchors.lock` — re-anchored to `config.py:122-253` & `policy.py:194-1394`)

## execution_models / ibkr_rebalance / L1_tws_walk_limit_and_races — retired (2026-11-16)
- Mode: fault-injection
- Spec: `execution_models/ibkr_rebalance/L1_tws_walk_limit_and_races/02_models/L1_TwsWalkLimitAndRaces_v2.tla` (pre-fix evidence frozen at `L1_TwsWalkLimitAndRaces.tla`)
- Properties checked: `TypeOK`, `LimitNeverExceedsSlippageCeiling`, `NoDuplicateOrderOnModify`, `NoModifyOnTerminalOrder`, `FillAccountingExact`, `CoarseTickWalkReachesNextTick` across both live (`8/6/25 bps`) and free 15m delayed (`25/15/60 bps`) protective walk bands (`delayed_quote \in BOOLEAN`; ALL PASS: 1,326 distinct states in `_v2.cfg`, 1,422 distinct states in `_v2_scale.cfg`; 20/20 actions covered across all 7 cfgs)
- Finding: retired(fixed, validated by L1_TwsWalkLimitAndRaces_v2)
- Fix: `broker.py:533-539` (`DoneStates` / `filled_now >= spec.quantity` pre-modify short-circuit for L1-A), `policy.py:1099-1102` (post-`get_quotes` `poll_order(wait_sec=0.0)` terminal break for L1-B), `broker.py:541-575` (`prev_lmt` snapshot + `Error 104` `errorEvent`/`trade.log` rollback for L1-C), `policy.py:1104-1111` (later-step coarse-tick walk lookahead `continue` for M1), and `policy.py:221-230, 267-287` (non-clamping delayed `25/15/60 bps` protective limits)
- Exports: `FillReport` exactness (`{"FILLED", "PARTIAL", "WORKING", "CANCELLED"}`) and crash-free walk-limit termination across live & delayed quotes
- Last synced: 2026-11-16 (`anchors.lock`)

## execution_models / ibkr_rebalance / L2_two_phase_cash_settlement — retired (2026-11-16)
- Mode: transcription
- Spec: `execution_models/ibkr_rebalance/L2_two_phase_cash_settlement/02_models/L2_TwoPhaseCashSettlement_v2.tla` (pre-fix evidence frozen at `L2_TwoPhaseCashSettlement.tla`)
- Properties checked: `TypeOK`, `BuyBudgetWithinCap`, `NoUnintendedVirtualFxPosition`, `ZeroMarginLoanSettledCash` (ALL PASS across one-way `USD -> HKD` domain per R6: 49 distinct states in `_v2.cfg`, 81 distinct states in `_v2_scale.cfg`; 12/12 actions covered)
- Finding: retired(fixed, validated by L2_TwoPhaseCashSettlement_v2; L2-B1 retired: refuted per LESSONS.md R6)
- Fix: `broker.py:622` (`Forex("USDHKD", exchange="FXCONV")` physical cash conversion for L2-A), `policy.py:1258-1279, 1337-1345` (`non_borrowed_usd = min(pre_fx_total, pre_fx_avail)` `[ZERO_MARGIN_FX_GUARD]` + rejected-FX `reserve_usd` handling for L2-B2; `policy.py:1008-1019` preserved per R6), and `policy.py:960-981` (`delayed_max_slippage_bps = 60.0` cash-cap outlay reservation for delayed quotes)
- Exports: non-borrowed `spendable` cash cap & `FXCONV` physical settlement guarantees
- Last synced: 2026-11-16 (`anchors.lock`)

## execution_models / ibkr_rebalance / L3_crash_retry_idempotency — retired (2026-11-16)
- Mode: fault-injection
- Spec: `execution_models/ibkr_rebalance/L3_crash_retry_idempotency/02_models/L3_CrashRetryIdempotency_v2.tla` (pre-fix evidence frozen at `L3_CrashRetryIdempotency.tla`)
- Properties checked: `TypeOK`, `NoDoubleOrderAcrossClientIds`, `NoStrandedUnfilledTargetOnRetry`, `NoScaledBuyPermanentUnderfill`, `NoDuplicateFxConversionOnRetry` (ALL PASS: 2,452 distinct states in `_v2.cfg`, 3,678 distinct states in `_v2_scale.cfg`; 18/18 actions covered across all 8 cfgs)
- Finding: retired(fixed, validated by L3_CrashRetryIdempotency_v2)
- Fix: `broker.py:345-364, 375-378` (`ib.reqAllOpenOrders()`, `DoneStates` filter, `CASH` `USD.HKD` open-order normalization for L3-A & L3-D), `policy.py:1194-1207, 1370-1383` (persist `fr.status == "FILLED"` only in `completed_intents` for L3-B), `policy.py:1059-1068, 1357-1358` (`f"{o.intent_id}:cap:{o.quantity}:{scaled_qty}"` capped buy intent IDs for L3-C), and `policy.py:1218-1301` (`fx_done_in_state` + `fx_open_working` check and immediate pre-Phase-B `save_execution_state` checkpoint for L3-D)
- Exports: terminal level of `ibkr_rebalance`
- Last synced: 2026-11-16 (`anchors.lock`)
