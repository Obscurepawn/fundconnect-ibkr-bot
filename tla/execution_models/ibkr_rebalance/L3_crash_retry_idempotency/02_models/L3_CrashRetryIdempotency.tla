--------------------- MODULE L3_CrashRetryIdempotency ---------------------
EXTENDS Integers, FiniteSets, TLC

\* ============================ var <-> code map ============================
\* CONSTANTS (read from src/fundconnect_bot/cli.py:464, 504-518 & policy.py:968-1009)
\*   InitSellQty       - initial held SGOV.US shares (100)     (src/fundconnect_bot/cli.py:253)
\*   TargetBuyQty      - planned target buy shares (100)       (src/fundconnect_bot/execution/policy.py:443)
\*   BootstrapFxUsd    - --bootstrap-fx-usd amount (30)        (src/fundconnect_bot/cli.py:504)
\*   Run1ClientId      - initial --client-id (26)              (src/fundconnect_bot/cli.py:517)
\*   Run2ClientIds     - retry --client-id domain ({26, 27})   (src/fundconnect_bot/cli.py:517)
\*   FxConfigs         - {"NO_FX", "WITH_FX"} CLI mode         (src/fundconnect_bot/cli.py:510)
\*
\* VARIABLES
\*   run_num           - CLI invocation number in {1, 2}       (src/fundconnect_bot/cli.py:414)
\*                       DENOTES: Run 1 (initial) vs Run 2 (crash/operator retry)
\*   step              - control point in execute_market_rebalance (src/fundconnect_bot/execution/policy.py:1074-1231)
\*                       DENOTES: {"BUILD_BATCH","PHASE_A","AFTER_PHASE_A","FX_AND_CAP","PHASE_B","AFTER_PHASE_B","BETWEEN_RUNS","RUN2_DONE"}
\*   run2_client_id    - clientId used by Run 2                (src/fundconnect_bot/cli.py:408, 517)
\*                       DENOTES: 26 (same clientId) or 27 (failover/manual cross-clientId)
\*   fx_cfg            - whether --bootstrap-fx-usd + --allow-fx-convert active (src/fundconnect_bot/execution/policy.py:1145-1146)
\*                       DENOTES: immutable CLI FX configuration across retry
\*   pos_sell          - broker held shares of Phase A symbol  (src/fundconnect_bot/execution/policy.py:1081)
\*                       DENOTES: live IBKR position for SGOV.US
\*   pos_buy           - broker held shares of Phase B symbol  (src/fundconnect_bot/execution/policy.py:1081)
\*                       DENOTES: live IBKR position for target equity
\*   cash_avail        - spendable USD cash at broker          (src/fundconnect_bot/execution/policy.py:1141, 1200)
\*                       DENOTES: realized non-borrowed USD cash available for FX & Phase B
\*   sell_orders       - TWS order records for Phase A symbol  (src/fundconnect_bot/execution/broker.py:340)
\*                       DENOTES: set of [id, client_id, rem_qty, status] on IBKR server
\*   buy_orders        - TWS order records for Phase B symbol  (src/fundconnect_bot/execution/broker.py:340)
\*                       DENOTES: set of [id, client_id, rem_qty, status] on IBKR server
\*   next_oid          - monotonic TWS order ID counter        (src/fundconnect_bot/execution/broker.py:457)
\*                       DENOTES: unique broker order identifier
\*   disk_completed    - ExecutionState.completed_intents keys (src/fundconnect_bot/execution/policy.py:143, 1079)
\*                       DENOTES: persisted intent_ids on disk in state/execution_state_*.json
\*   batch_sell_qty    - emitted Phase A sell OrderSpec.quantity (src/fundconnect_bot/execution/policy.py:547)
\*                       DENOTES: planned sell qty after working_qty & completed_intents filter
\*   batch_buy_qty     - uncapped Phase B buy OrderSpec.quantity (src/fundconnect_bot/execution/policy.py:547)
\*                       DENOTES: planned buy qty before apply_cash_cap_to_buys
\*   capped_buy_qty    - capped Phase B buy OrderSpec.quantity (src/fundconnect_bot/execution/policy.py:1005, 1202)
\*                       DENOTES: post-cash-cap buy qty (retaining BUY_INTENT at policy.py:1005)
\*   phase_a_status    - FillReport.status of Phase A order    (src/fundconnect_bot/execution/policy.py:1116, 1125)
\*                       DENOTES: {"NONE","SKIPPED","FILLED","PARTIAL","WORKING"}
\*   phase_b_status    - FillReport.status of Phase B order    (src/fundconnect_bot/execution/policy.py:1208, 1217)
\*                       DENOTES: {"NONE","SKIPPED","FILLED","WORKING"}
\*   run1_scaled_buy   - witness flag: Run 1 scaled BUY_INTENT (src/fundconnect_bot/execution/policy.py:1000-1005)
\*                       DENOTES: TRUE iff apply_cash_cap_to_buys reduced batch_buy_qty in Run 1
\*   fx_converted_total- cumulative USD sold via convert_usd_to_hkd (src/fundconnect_bot/execution/policy.py:1147)
\*                       DENOTES: total USD converted across Run 1 + Run 2 for this session
\* =========================================================================

CONSTANTS
    InitSellQty,
    TargetBuyQty,
    BootstrapFxUsd,
    Run1ClientId,
    Run2ClientIds,
    FxConfigs

VARIABLES
    run_num,
    step,
    run2_client_id,
    fx_cfg,
    pos_sell,
    pos_buy,
    cash_avail,
    sell_orders,
    buy_orders,
    next_oid,
    disk_completed,
    batch_sell_qty,
    batch_buy_qty,
    capped_buy_qty,
    phase_a_status,
    phase_b_status,
    run1_scaled_buy,
    fx_converted_total

vars == <<
    run_num, step, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail,
    sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty,
    batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status,
    run1_scaled_buy, fx_converted_total
>>

Max0(x) == IF x > 0 THEN x ELSE 0
Min2(a, b) == IF a < b THEN a ELSE b

ActiveClientId == IF run_num = 1 THEN Run1ClientId ELSE run2_client_id

RECURSIVE SumRem(_)
SumRem(oset) ==
    IF oset = {} THEN 0
    ELSE LET o == CHOOSE x \in oset : TRUE
         IN (IF o.status = "Working" THEN o.rem_qty ELSE 0) + SumRem(oset \ {o})

\* Exact transcription of IbAsyncBrokerAdapter.get_open_orders + working_by_sym_side:
\* broker.py:338 calls ib.reqOpenOrders(), returning ONLY open orders for ActiveClientId!
\* (src/fundconnect_bot/execution/broker.py:336-369, src/fundconnect_bot/execution/policy.py:316-320)
VisibleWorkingQty(oset, cid) ==
    SumRem({ o \in oset : o.client_id = cid })

TotalWorkingSell == SumRem(sell_orders)
TotalWorkingBuy  == SumRem(buy_orders)

\* ---- Initial state ----
Init ==
    /\ run_num            = 1
    /\ step               = "BUILD_BATCH"
    /\ run2_client_id     \in Run2ClientIds
    /\ fx_cfg             \in FxConfigs
    /\ pos_sell           = InitSellQty
    /\ pos_buy            = 0
    /\ cash_avail         = 0
    /\ sell_orders        = {}
    /\ buy_orders         = {}
    /\ next_oid           = 1
    /\ disk_completed     = {}
    /\ batch_sell_qty     = 0
    /\ batch_buy_qty      = 0
    /\ capped_buy_qty     = 0
    /\ phase_a_status     = "NONE"
    /\ phase_b_status     = "NONE"
    /\ run1_scaled_buy    = FALSE
    /\ fx_converted_total = 0

\* ---- Step 1: Load state, query get_open_orders(cid), build batch (src/fundconnect_bot/execution/policy.py:1074-1112) ----

BuildBatch ==
    \* policy.py:442-448 subtracts working_qty visible to ActiveClientId;
    \* policy.py:535-537 skips intent if intent_id \in completed_intents!
    /\ step = "BUILD_BATCH"
    /\ LET cid      == ActiveClientId
           vis_sell == VisibleWorkingQty(sell_orders, cid)
           vis_buy  == VisibleWorkingQty(buy_orders, cid)
           raw_sell == Max0(pos_sell - vis_sell)
           raw_buy  == Max0((TargetBuyQty - pos_buy) - vis_buy)
           emit_s   == IF "SELL_INTENT" \in disk_completed THEN 0 ELSE raw_sell
           emit_b   == IF "BUY_INTENT"  \in disk_completed THEN 0 ELSE raw_buy
       IN /\ batch_sell_qty' = emit_s
          /\ batch_buy_qty'  = emit_b
          /\ capped_buy_qty' = 0
          /\ phase_a_status' = "NONE"
          /\ phase_b_status' = "NONE"
          /\ step'           = "PHASE_A"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, disk_completed, run1_scaled_buy, fx_converted_total>>

\* ---- Step 2: Phase A execution outcomes (src/fundconnect_bot/execution/policy.py:1116-1122) ----

PhaseASkip ==
    /\ step = "PHASE_A"
    /\ batch_sell_qty = 0
    /\ phase_a_status' = "SKIPPED"
    /\ step'           = "AFTER_PHASE_A"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_status, run1_scaled_buy, fx_converted_total>>

PhaseAFillFull ==
    \* Phase A sell fills completely during walk (src/fundconnect_bot/execution/policy.py:1116-1122)
    /\ step = "PHASE_A"
    /\ batch_sell_qty > 0
    /\ pos_sell >= batch_sell_qty
    /\ pos_sell'       = pos_sell - batch_sell_qty
    /\ cash_avail'     = cash_avail + batch_sell_qty
    /\ phase_a_status' = "FILLED"
    /\ step'           = "AFTER_PHASE_A"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_buy, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_status, run1_scaled_buy, fx_converted_total>>

PhaseAFillPartial ==
    \* Phase A sell fills 50% in-session and leaves 50% Working on TWS (src/fundconnect_bot/execution/broker.py:420-421)
    /\ step = "PHASE_A"
    /\ run_num = 1
    /\ batch_sell_qty = InitSellQty
    /\ LET half == InitSellQty \div 2
           rem  == InitSellQty - half
           ord  == [id |-> next_oid, client_id |-> ActiveClientId, rem_qty |-> rem, status |-> "Working"]
       IN /\ pos_sell'       = pos_sell - half
          /\ cash_avail'     = cash_avail + half
          /\ sell_orders'    = sell_orders \cup {ord}
          /\ next_oid'       = next_oid + 1
          /\ phase_a_status' = "PARTIAL"
          /\ step'           = "AFTER_PHASE_A"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_buy, buy_orders, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_status, run1_scaled_buy, fx_converted_total>>

PhaseAStayWorking ==
    \* Phase A sell stays WORKING on TWS after walk steps expire (src/fundconnect_bot/execution/broker.py:422-423)
    /\ step = "PHASE_A"
    /\ batch_sell_qty > 0
    /\ LET ord == [id |-> next_oid, client_id |-> ActiveClientId, rem_qty |-> batch_sell_qty, status |-> "Working"]
       IN /\ sell_orders'    = sell_orders \cup {ord}
          /\ next_oid'       = next_oid + 1
          /\ phase_a_status' = "WORKING"
          /\ step'           = "AFTER_PHASE_A"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, buy_orders, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 3: Phase A checkpoint vs crash before save_execution_state (src/fundconnect_bot/execution/policy.py:1124-1137) ----

CrashBeforePhaseASave ==
    \* Process crash or L1 unhandled AssertionError before save_execution_state (src/fundconnect_bot/execution/policy.py:1122-1128)
    /\ step = "AFTER_PHASE_A"
    /\ run_num = 1
    /\ step' = "BETWEEN_RUNS"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

SavePhaseAAndProceedToFx ==
    \* policy.py:1125-1126 records FILLED, PARTIAL, and WORKING in completed_intents!
    /\ step = "AFTER_PHASE_A"
    /\ disk_completed' = IF phase_a_status \in {"FILLED", "PARTIAL", "WORKING"}
                         THEN disk_completed \cup {"SELL_INTENT"}
                         ELSE disk_completed
    /\ step'           = "FX_AND_CAP"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 4: Optional FX conversion + apply_cash_cap_to_buys (src/fundconnect_bot/execution/policy.py:1145-1205, 990-1008) ----

FxAndCapBuys ==
    \* policy.py:1145-1148 calls convert_usd_to_hkd(bootstrap_fx_usd) without checking disk_completed!
    \* policy.py:1005 down-scales quantity via replace(o, quantity=scaled_qty), preserving o.intent_id ("BUY_INTENT")!
    /\ step = "FX_AND_CAP"
    /\ LET do_fx    == (fx_cfg = "WITH_FX" /\ BootstrapFxUsd > 0 /\ cash_avail >= BootstrapFxUsd)
           fx_amt   == IF do_fx THEN BootstrapFxUsd ELSE 0
           rem_cash == cash_avail - fx_amt
           capped   == Min2(batch_buy_qty, rem_cash)
           scaled   == (run_num = 1 /\ capped > 0 /\ capped < batch_buy_qty /\ fx_cfg = "NO_FX")
       IN /\ cash_avail'         = rem_cash
          /\ fx_converted_total' = fx_converted_total + fx_amt
          /\ capped_buy_qty'     = capped
          /\ run1_scaled_buy'    = (run1_scaled_buy \/ scaled)
          /\ step'               = "PHASE_B"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, phase_a_status, phase_b_status>>

\* ---- Step 5: Phase B execution outcomes (src/fundconnect_bot/execution/policy.py:1208-1214) ----

PhaseBSkip ==
    /\ step = "PHASE_B"
    /\ capped_buy_qty = 0
    /\ phase_b_status' = "SKIPPED"
    /\ step'           = "AFTER_PHASE_B"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, run1_scaled_buy, fx_converted_total>>

PhaseBFill ==
    /\ step = "PHASE_B"
    /\ capped_buy_qty > 0
    /\ cash_avail >= capped_buy_qty
    /\ pos_buy'        = pos_buy + capped_buy_qty
    /\ cash_avail'     = cash_avail - capped_buy_qty
    /\ phase_b_status' = "FILLED"
    /\ step'           = "AFTER_PHASE_B"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, run1_scaled_buy, fx_converted_total>>

PhaseBStayWorking ==
    /\ step = "PHASE_B"
    /\ capped_buy_qty > 0
    /\ LET ord == [id |-> next_oid, client_id |-> ActiveClientId, rem_qty |-> capped_buy_qty, status |-> "Working"]
       IN /\ buy_orders'     = buy_orders \cup {ord}
          /\ next_oid'       = next_oid + 1
          /\ phase_b_status' = "WORKING"
          /\ step'           = "AFTER_PHASE_B"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 6: Phase B checkpoint vs crash before save_execution_state (src/fundconnect_bot/execution/policy.py:1216-1229) ----

CrashBeforePhaseBSave ==
    \* Interruption after Phase A + FX + Phase B submission before final save_execution_state (src/fundconnect_bot/execution/policy.py:1214-1220)
    /\ step = "AFTER_PHASE_B"
    /\ run_num = 1
    /\ step' = "BETWEEN_RUNS"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

SavePhaseBAndFinishRun ==
    \* policy.py:1217-1218 records FILLED, PARTIAL, and WORKING in completed_intents!
    /\ step = "AFTER_PHASE_B"
    /\ disk_completed' = IF phase_b_status \in {"FILLED", "PARTIAL", "WORKING"}
                         THEN disk_completed \cup {"BUY_INTENT"}
                         ELSE disk_completed
    /\ step'           = IF run_num = 1 THEN "BETWEEN_RUNS" ELSE "RUN2_DONE"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 7: Inter-run TWS venue events & CLI retry (src/fundconnect_bot/cli.py:378-430) ----

InterRunTwsFillWorking ==
    \* Open Working order from Run 1 fills on TWS before Run 2 starts
    /\ step = "BETWEEN_RUNS"
    /\ \/ \E o \in sell_orders :
            /\ o.status = "Working"
            /\ pos_sell >= o.rem_qty
            /\ pos_sell'    = pos_sell - o.rem_qty
            /\ cash_avail'  = cash_avail + o.rem_qty
            /\ sell_orders' = (sell_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Filled"]}
            /\ UNCHANGED <<pos_buy, buy_orders>>
       \/ \E o \in buy_orders :
            /\ o.status = "Working"
            /\ cash_avail >= o.rem_qty
            /\ pos_buy'     = pos_buy + o.rem_qty
            /\ cash_avail'  = cash_avail - o.rem_qty
            /\ buy_orders'  = (buy_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Filled"]}
            /\ UNCHANGED <<pos_sell, sell_orders>>
    /\ UNCHANGED <<run_num, step, run2_client_id, fx_cfg, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

InterRunTwsCancelWorking ==
    \* Open Working order from Run 1 is cancelled by TWS / exchange / DAY expiry (ApiCancelled / Inactive)
    /\ step = "BETWEEN_RUNS"
    /\ \/ \E o \in sell_orders :
            /\ o.status = "Working"
            /\ sell_orders' = (sell_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Cancelled"]}
            /\ UNCHANGED buy_orders
       \/ \E o \in buy_orders :
            /\ o.status = "Working"
            /\ buy_orders'  = (buy_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Cancelled"]}
            /\ UNCHANGED sell_orders
    /\ UNCHANGED <<run_num, step, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

StartRetryRun2 ==
    \* Operator or systemd service invokes `fundconnect-bot execute --live` for Run 2 (src/fundconnect_bot/cli.py:414-424)
    /\ step = "BETWEEN_RUNS"
    /\ run_num' = 2
    /\ step'    = "BUILD_BATCH"
    /\ UNCHANGED <<run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

Run2DoneStutter ==
    /\ step = "RUN2_DONE"
    /\ UNCHANGED vars

Next ==
    \/ BuildBatch
    \/ PhaseASkip
    \/ PhaseAFillFull
    \/ PhaseAFillPartial
    \/ PhaseAStayWorking
    \/ CrashBeforePhaseASave
    \/ SavePhaseAAndProceedToFx
    \/ FxAndCapBuys
    \/ PhaseBSkip
    \/ PhaseBFill
    \/ PhaseBStayWorking
    \/ CrashBeforePhaseBSave
    \/ SavePhaseBAndFinishRun
    \/ InterRunTwsFillWorking
    \/ InterRunTwsCancelWorking
    \/ StartRetryRun2
    \/ Run2DoneStutter

Spec == Init /\ [][Next]_vars

\* =========================================================================
\* Developer-Expectation Invariants
\* =========================================================================

TypeOK ==
    /\ run_num \in {1, 2}
    /\ step \in {"BUILD_BATCH", "PHASE_A", "AFTER_PHASE_A", "FX_AND_CAP", "PHASE_B", "AFTER_PHASE_B", "BETWEEN_RUNS", "RUN2_DONE"}
    /\ run2_client_id \in Run2ClientIds
    /\ fx_cfg \in FxConfigs
    /\ pos_sell >= 0
    /\ pos_buy >= 0
    /\ cash_avail >= 0
    /\ disk_completed \subseteq {"SELL_INTENT", "BUY_INTENT"}

\* 1. NoDoubleOrderAcrossClientIds:
\*    Cumulative executed + open working shares across ALL TWS clientIds never exceed planned targets.
NoDoubleOrderAcrossClientIds ==
    /\ (InitSellQty - pos_sell) + TotalWorkingSell <= InitSellQty
    /\ pos_buy + TotalWorkingBuy <= TargetBuyQty

\* 2. NoStrandedUnfilledTargetOnRetry:
\*    After Run 2 completes (without FX deduction or partial-sell scaling), no cancelled/expired
\*    WORKING/PARTIAL intent from Run 1 leaves the portfolio stranded with 0 working orders and
\*    unfilled target shares.
NoStrandedUnfilledTargetOnRetry ==
    (step = "RUN2_DONE" /\ fx_cfg = "NO_FX" /\ ~run1_scaled_buy) =>
        /\ (TotalWorkingSell = 0 => pos_sell = 0)
        /\ ((pos_sell = 0 /\ TotalWorkingBuy = 0) => pos_buy = TargetBuyQty)

\* 3. NoScaledBuyPermanentUnderfill:
\*    If Run 1 down-scaled Phase B buy due to partial Phase A sell proceeds, and Phase A finishes
\*    filling before Run 2 (`pos_sell = 0`), Run 2 must not permanently underfill `TargetBuyQty`
\*    due to the scaled Run 1 fill having burned `BUY_INTENT` in `completed_intents`.
NoScaledBuyPermanentUnderfill ==
    (step = "RUN2_DONE" /\ fx_cfg = "NO_FX" /\ run1_scaled_buy /\ pos_sell = 0 /\ TotalWorkingBuy = 0)
        => (pos_buy = TargetBuyQty)

\* 4. NoDuplicateFxConversionOnRetry:
\*    Retrying `execute --bootstrap-fx-usd B --allow-fx-convert` for the same session never
\*    converts more than `BootstrapFxUsd` cumulative USD.
NoDuplicateFxConversionOnRetry ==
    /\ (fx_cfg = "NO_FX"   => fx_converted_total = 0)
    /\ (fx_cfg = "WITH_FX" => fx_converted_total <= BootstrapFxUsd)
=============================================================================
