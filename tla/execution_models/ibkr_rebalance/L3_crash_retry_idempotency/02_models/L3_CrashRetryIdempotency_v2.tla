-------------------- MODULE L3_CrashRetryIdempotency_v2 --------------------
EXTENDS Integers, FiniteSets, TLC

\* ============================ var <-> code map ============================
\* CONSTANTS (read from src/fundconnect_bot/cli.py:564-609 & src/fundconnect_bot/execution/policy.py:1022-1071)
\*   InitSellQty       - initial held SGOV.US shares (100)     (src/fundconnect_bot/cli.py:321)
\*   TargetBuyQty      - planned target buy shares (100)       (src/fundconnect_bot/execution/policy.py:476)
\*   BootstrapFxUsd    - --bootstrap-fx-usd amount (30)        (src/fundconnect_bot/cli.py:587)
\*   Run1ClientId      - initial --client-id (26)              (src/fundconnect_bot/cli.py:600)
\*   Run2ClientIds     - retry --client-id domain ({26, 27})   (src/fundconnect_bot/cli.py:600)
\*   FxConfigs         - {"NO_FX", "WITH_FX"} CLI mode         (src/fundconnect_bot/cli.py:593)
\*
\* VARIABLES
\*   run_num           - CLI invocation number in {1, 2}       (src/fundconnect_bot/cli.py:483)
\*                       DENOTES: Run 1 (initial) vs Run 2 (crash/operator retry)
\*   step              - control point in execute_market_rebalance (src/fundconnect_bot/execution/policy.py:1144-1394)
\*                       DENOTES: {"BUILD_BATCH","PHASE_A","AFTER_PHASE_A","FX_AND_CAP","PHASE_B","AFTER_PHASE_B","BETWEEN_RUNS","RUN2_DONE"}
\*   run2_client_id    - clientId used by Run 2                (src/fundconnect_bot/cli.py:476, 600)
\*                       DENOTES: 26 (same clientId) or 27 (failover/manual cross-clientId)
\*   fx_cfg            - whether --bootstrap-fx-usd + --allow-fx-convert active (src/fundconnect_bot/execution/policy.py:1217, 1257)
\*                       DENOTES: immutable CLI FX configuration across retry
\*   pos_sell          - broker held shares of Phase A symbol  (src/fundconnect_bot/execution/policy.py:1151)
\*                       DENOTES: live IBKR position for SGOV.US
\*   pos_buy           - broker held shares of Phase B symbol  (src/fundconnect_bot/execution/policy.py:1151)
\*                       DENOTES: live IBKR position for target equity
\*   cash_avail        - non-borrowed USD cash at broker       (src/fundconnect_bot/execution/policy.py:1211, 1262-1266, 1352)
\*                       DENOTES: realized non-borrowed USD cash available for FX & Phase B
\*   sell_orders       - TWS order records for Phase A symbol  (src/fundconnect_bot/execution/broker.py:372-422)
\*                       DENOTES: set of [id, client_id, rem_qty, status] on IBKR server
\*   buy_orders        - TWS order records for Phase B symbol  (src/fundconnect_bot/execution/broker.py:372-422)
\*                       DENOTES: set of [id, client_id, rem_qty, status] on IBKR server
\*   fx_orders         - TWS CASH USD.HKD order records        (src/fundconnect_bot/execution/broker.py:387-393)
\*                       DENOTES: open/terminal USD.HKD FX orders across all clientIds
\*   next_oid          - monotonic TWS order ID counter        (src/fundconnect_bot/execution/broker.py:553)
\*                       DENOTES: unique broker order identifier
\*   disk_completed    - ExecutionState.completed_intents keys (src/fundconnect_bot/execution/policy.py:143, 1149)
\*                       DENOTES: persisted intent_ids on disk in state/execution_state_*.json
\*   batch_sell_qty    - emitted Phase A sell OrderSpec.quantity (src/fundconnect_bot/execution/policy.py:580)
\*                       DENOTES: planned sell qty after reqAllOpenOrders working_qty & completed_intents filter
\*   batch_buy_qty     - uncapped Phase B buy OrderSpec.quantity (src/fundconnect_bot/execution/policy.py:580)
\*                       DENOTES: planned buy qty before apply_cash_cap_to_buys
\*   capped_buy_qty    - capped Phase B buy OrderSpec.quantity (src/fundconnect_bot/execution/policy.py:1059-1068, 1354-1359)
\*                       DENOTES: post-cash-cap buy qty after completed_intents deduplication
\*   phase_b_intent    - OrderSpec.intent_id of capped Phase B buy (src/fundconnect_bot/execution/policy.py:1060-1067)
\*                       DENOTES: base BUY intent vs `:cap:{orig}:{scaled}` intent
\*   phase_a_status    - FillReport.status of Phase A order    (src/fundconnect_bot/execution/policy.py:1186, 1195)
\*                       DENOTES: {"NONE","SKIPPED","FILLED","PARTIAL","WORKING"}
\*   phase_b_status    - FillReport.status of Phase B order    (src/fundconnect_bot/execution/policy.py:1362, 1371)
\*                       DENOTES: {"NONE","SKIPPED","FILLED","WORKING"}
\*   run1_scaled_buy   - witness flag: Run 1 scaled BUY_INTENT (src/fundconnect_bot/execution/policy.py:1059-1067)
\*                       DENOTES: TRUE iff apply_cash_cap_to_buys reduced batch_buy_qty in Run 1
\*   fx_converted_total- cumulative USD sold via convert_usd_to_hkd (src/fundconnect_bot/execution/policy.py:1275)
\*                       DENOTES: total filled USD converted across Run 1 + Run 2 for this session
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
    fx_orders,
    next_oid,
    disk_completed,
    batch_sell_qty,
    batch_buy_qty,
    capped_buy_qty,
    phase_b_intent,
    phase_a_status,
    phase_b_status,
    run1_scaled_buy,
    fx_converted_total

vars == <<
    run_num, step, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail,
    sell_orders, buy_orders, fx_orders, next_oid, disk_completed,
    batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent,
    phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total
>>

Max0(x) == IF x > 0 THEN x ELSE 0
Min2(a, b) == IF a < b THEN a ELSE b

ActiveClientId == IF run_num = 1 THEN Run1ClientId ELSE run2_client_id

\* Homogeneous intent_id descriptors matching src/fundconnect_bot/execution/policy.py:568, 1062, 1218
SellIntent == [kind |-> "SELL", orig |-> 0, scaled |-> 0]
FxIntent   == [kind |-> "FX",   orig |-> BootstrapFxUsd, scaled |-> BootstrapFxUsd]
BuyIntent  == [kind |-> "BUY",  orig |-> 0, scaled |-> 0]
BuyCapIntent(orig_q, scaled_q) == [kind |-> "BUY_CAP", orig |-> orig_q, scaled |-> scaled_q]

RECURSIVE SumRem(_)
SumRem(oset) ==
    IF oset = {} THEN 0
    ELSE LET o == CHOOSE x \in oset : TRUE
         IN (IF o.status = "Working" THEN o.rem_qty ELSE 0) + SumRem(oset \ {o})

\* Fix L3-A: IbAsyncBrokerAdapter.get_open_orders calls ib.reqAllOpenOrders() (src/fundconnect_bot/execution/broker.py:374-378),
\* returning open orders across ALL clientIds (including prior crashed sessions and manual TWS orders),
\* and normalizes CASH USD.HKD open orders to symbol="USD.HKD" (src/fundconnect_bot/execution/broker.py:387-393).
VisibleWorkingQty(oset) == SumRem(oset)

TotalWorkingSell == SumRem(sell_orders)
TotalWorkingBuy  == SumRem(buy_orders)
WorkingFxUsd     == SumRem(fx_orders)

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
    /\ fx_orders          \in (IF fx_cfg = "WITH_FX"
                               THEN { {}, {[id |-> 0, client_id |-> 99, rem_qty |-> BootstrapFxUsd, status |-> "Working"]} }
                               ELSE { {} })
    /\ next_oid           = 1
    /\ disk_completed     = {}
    /\ batch_sell_qty     = 0
    /\ batch_buy_qty      = 0
    /\ capped_buy_qty     = 0
    /\ phase_b_intent     = BuyIntent
    /\ phase_a_status     = "NONE"
    /\ phase_b_status     = "NONE"
    /\ run1_scaled_buy    = FALSE
    /\ fx_converted_total = 0

\* ---- Step 1: Load state, query reqAllOpenOrders(), build batch (src/fundconnect_bot/execution/policy.py:1144-1183) ----

BuildBatch ==
    \* src/fundconnect_bot/execution/policy.py:475-481 subtracts working_qty across all clientIds (src/fundconnect_bot/execution/broker.py:374-378);
    \* src/fundconnect_bot/execution/policy.py:568-570 skips intent if base intent_id \in completed_intents.
    /\ step = "BUILD_BATCH"
    /\ LET vis_sell == VisibleWorkingQty(sell_orders)
           vis_buy  == VisibleWorkingQty(buy_orders)
           raw_sell == Max0(pos_sell - vis_sell)
           raw_buy  == Max0((TargetBuyQty - pos_buy) - vis_buy)
           emit_s   == IF SellIntent \in disk_completed THEN 0 ELSE raw_sell
           emit_b   == IF BuyIntent  \in disk_completed THEN 0 ELSE raw_buy
       IN /\ batch_sell_qty' = emit_s
          /\ batch_buy_qty'  = emit_b
          /\ capped_buy_qty' = 0
          /\ phase_b_intent' = BuyIntent
          /\ phase_a_status' = "NONE"
          /\ phase_b_status' = "NONE"
          /\ step'           = "PHASE_A"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, run1_scaled_buy, fx_converted_total>>

\* ---- Step 2: Phase A execution outcomes (src/fundconnect_bot/execution/policy.py:1186-1192) ----

PhaseASkip ==
    /\ step = "PHASE_A"
    /\ batch_sell_qty = 0
    /\ phase_a_status' = "SKIPPED"
    /\ step'           = "AFTER_PHASE_A"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_b_status, run1_scaled_buy, fx_converted_total>>

PhaseAFillFull ==
    \* Phase A sell fills completely during walk (src/fundconnect_bot/execution/policy.py:1186-1192)
    /\ step = "PHASE_A"
    /\ batch_sell_qty > 0
    /\ pos_sell >= batch_sell_qty
    /\ pos_sell'       = pos_sell - batch_sell_qty
    /\ cash_avail'     = cash_avail + batch_sell_qty
    /\ phase_a_status' = "FILLED"
    /\ step'           = "AFTER_PHASE_A"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_buy, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_b_status, run1_scaled_buy, fx_converted_total>>

PhaseAFillPartial ==
    \* Phase A sell fills 50% in-session and leaves 50% Working on TWS (src/fundconnect_bot/execution/broker.py:516-517)
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
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_buy, buy_orders, fx_orders, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_b_status, run1_scaled_buy, fx_converted_total>>

PhaseAStayWorking ==
    \* Phase A sell stays WORKING on TWS after walk steps expire (src/fundconnect_bot/execution/broker.py:518-519)
    /\ step = "PHASE_A"
    /\ batch_sell_qty > 0
    /\ LET ord == [id |-> next_oid, client_id |-> ActiveClientId, rem_qty |-> batch_sell_qty, status |-> "Working"]
       IN /\ sell_orders'    = sell_orders \cup {ord}
          /\ next_oid'       = next_oid + 1
          /\ phase_a_status' = "WORKING"
          /\ step'           = "AFTER_PHASE_A"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, buy_orders, fx_orders, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_b_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 3: Phase A checkpoint vs crash before save_execution_state (src/fundconnect_bot/execution/policy.py:1194-1207) ----

CrashBeforePhaseASave ==
    \* Process crash / socket drop during Phase A before save_execution_state (src/fundconnect_bot/execution/policy.py:1192-1198)
    /\ step = "AFTER_PHASE_A"
    /\ run_num = 1
    /\ step' = "BETWEEN_RUNS"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

SavePhaseAAndProceedToFx ==
    \* Fix L3-B: src/fundconnect_bot/execution/policy.py:1194-1207 persists ONLY `fr.status == "FILLED"` in completed_intents
    \* (never WORKING or PARTIAL, allowing clean retry if TWS later cancels an unfilled order).
    /\ step = "AFTER_PHASE_A"
    /\ disk_completed' = IF phase_a_status = "FILLED"
                         THEN disk_completed \cup {SellIntent}
                         ELSE disk_completed
    /\ step'           = "FX_AND_CAP"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 4: Optional FX conversion + immediate FX checkpoint + apply_cash_cap_to_buys (src/fundconnect_bot/execution/policy.py:1217-1359) ----

FxAndCapBuys ==
    \* Fix L3-D: src/fundconnect_bot/execution/policy.py:1218-1301 checks fx_done_in_state (`FxIntent \in disk_completed`) and
    \* fx_open_working (`WorkingFxUsd > 0`), gates FX on non_borrowed_usd >= BootstrapFxUsd (src/fundconnect_bot/execution/policy.py:1266-1273),
    \* and immediately checkpoints `FxIntent` to `disk_completed` BEFORE Phase B (src/fundconnect_bot/execution/policy.py:1291-1301)!
    \* Fix L3-C: src/fundconnect_bot/execution/policy.py:1059-1067 assigns `BuyCapIntent(orig, scaled)` whenever `scaled < orig`, and
    \* src/fundconnect_bot/execution/policy.py:1357-1358 skips any capped_spec whose `intent_id` is already in `disk_completed`.
    /\ step = "FX_AND_CAP"
    /\ LET fx_done   == (FxIntent \in disk_completed)
           fx_open   == (WorkingFxUsd > 0)
           do_fx     == (fx_cfg = "WITH_FX" /\ ~fx_done /\ ~fx_open /\ BootstrapFxUsd > 0 /\ cash_avail >= BootstrapFxUsd)
           fx_amt    == IF do_fx THEN BootstrapFxUsd ELSE 0
           rem_cash  == cash_avail - fx_amt
           res_usd   == IF fx_cfg = "WITH_FX" /\ ~fx_done /\ (fx_open \/ cash_avail < BootstrapFxUsd)
                        THEN BootstrapFxUsd
                        ELSE 0
           spendable == Max0(rem_cash - res_usd)
           new_disk  == IF do_fx THEN disk_completed \cup {FxIntent} ELSE disk_completed
           raw_cap   == Min2(batch_buy_qty, spendable)
           b_intent  == IF raw_cap > 0 /\ raw_cap < batch_buy_qty
                        THEN BuyCapIntent(batch_buy_qty, raw_cap)
                        ELSE BuyIntent
           eff_cap   == IF raw_cap = 0 \/ b_intent \in new_disk THEN 0 ELSE raw_cap
           scaled    == (run_num = 1 /\ eff_cap > 0 /\ eff_cap < batch_buy_qty /\ fx_cfg = "NO_FX")
       IN /\ cash_avail'         = rem_cash
          /\ fx_converted_total' = fx_converted_total + fx_amt
          /\ disk_completed'     = new_disk
          /\ capped_buy_qty'     = eff_cap
          /\ phase_b_intent'     = b_intent
          /\ run1_scaled_buy'    = (run1_scaled_buy \/ scaled)
          /\ step'               = "PHASE_B"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, sell_orders, buy_orders, fx_orders, next_oid, batch_sell_qty, batch_buy_qty, phase_a_status, phase_b_status>>

\* ---- Step 5: Phase B execution outcomes (src/fundconnect_bot/execution/policy.py:1362-1368) ----

PhaseBSkip ==
    /\ step = "PHASE_B"
    /\ capped_buy_qty = 0
    /\ phase_b_status' = "SKIPPED"
    /\ step'           = "AFTER_PHASE_B"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, run1_scaled_buy, fx_converted_total>>

PhaseBFill ==
    /\ step = "PHASE_B"
    /\ capped_buy_qty > 0
    /\ cash_avail >= capped_buy_qty
    /\ pos_buy'        = pos_buy + capped_buy_qty
    /\ cash_avail'     = cash_avail - capped_buy_qty
    /\ phase_b_status' = "FILLED"
    /\ step'           = "AFTER_PHASE_B"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, run1_scaled_buy, fx_converted_total>>

PhaseBStayWorking ==
    /\ step = "PHASE_B"
    /\ capped_buy_qty > 0
    /\ LET ord == [id |-> next_oid, client_id |-> ActiveClientId, rem_qty |-> capped_buy_qty, status |-> "Working"]
       IN /\ buy_orders'     = buy_orders \cup {ord}
          /\ next_oid'       = next_oid + 1
          /\ phase_b_status' = "WORKING"
          /\ step'           = "AFTER_PHASE_B"
          /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, fx_orders, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 6: Phase B checkpoint vs crash before save_execution_state (src/fundconnect_bot/execution/policy.py:1369-1383) ----

CrashBeforePhaseBSave ==
    \* Interruption during Phase B before final save_execution_state (src/fundconnect_bot/execution/policy.py:1368-1374);
    \* note FxIntent was already persisted at src/fundconnect_bot/execution/policy.py:1292-1301!
    /\ step = "AFTER_PHASE_B"
    /\ run_num = 1
    /\ step' = "BETWEEN_RUNS"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

SavePhaseBAndFinishRun ==
    \* Fix L3-B & L3-C: src/fundconnect_bot/execution/policy.py:1370-1383 persists `phase_b_intent` ONLY when `phase_b_status = "FILLED"`.
    /\ step = "AFTER_PHASE_B"
    /\ disk_completed' = IF phase_b_status = "FILLED"
                         THEN disk_completed \cup {phase_b_intent}
                         ELSE disk_completed
    /\ step'           = IF run_num = 1 THEN "BETWEEN_RUNS" ELSE "RUN2_DONE"
    /\ UNCHANGED <<run_num, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

\* ---- Step 7: Inter-run TWS venue events & CLI retry (src/fundconnect_bot/cli.py:446-498) ----

InterRunTwsFillWorking ==
    \* Open Working order from Run 1 fills on TWS before Run 2 starts
    /\ step = "BETWEEN_RUNS"
    /\ \/ \E o \in sell_orders :
            /\ o.status = "Working"
            /\ pos_sell >= o.rem_qty
            /\ pos_sell'    = pos_sell - o.rem_qty
            /\ cash_avail'  = cash_avail + o.rem_qty
            /\ sell_orders' = (sell_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Filled"]}
            /\ UNCHANGED <<pos_buy, buy_orders, fx_orders, fx_converted_total>>
       \/ \E o \in buy_orders :
            /\ o.status = "Working"
            /\ cash_avail >= o.rem_qty
            /\ pos_buy'     = pos_buy + o.rem_qty
            /\ cash_avail'  = cash_avail - o.rem_qty
            /\ buy_orders'  = (buy_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Filled"]}
            /\ UNCHANGED <<pos_sell, sell_orders, fx_orders, fx_converted_total>>
    /\ UNCHANGED <<run_num, step, run2_client_id, fx_cfg, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy>>

InterRunTwsCancelWorking ==
    \* Open Working order is cancelled by TWS / exchange / DAY expiry (ApiCancelled / Inactive)
    /\ step = "BETWEEN_RUNS"
    /\ \/ \E o \in sell_orders :
            /\ o.status = "Working"
            /\ sell_orders' = (sell_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Cancelled"]}
            /\ UNCHANGED <<buy_orders, fx_orders>>
       \/ \E o \in buy_orders :
            /\ o.status = "Working"
            /\ buy_orders'  = (buy_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Cancelled"]}
            /\ UNCHANGED <<sell_orders, fx_orders>>
       \/ \E o \in fx_orders :
            /\ o.status = "Working"
            /\ fx_orders'   = (fx_orders \ {o}) \cup {[o EXCEPT !.rem_qty = 0, !.status = "Cancelled"]}
            /\ UNCHANGED <<sell_orders, buy_orders>>
    /\ UNCHANGED <<run_num, step, run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

StartRetryRun2 ==
    \* Operator or systemd service invokes `fundconnect-bot execute --live` for Run 2 (src/fundconnect_bot/cli.py:483-493)
    /\ step = "BETWEEN_RUNS"
    /\ run_num' = 2
    /\ step'    = "BUILD_BATCH"
    /\ UNCHANGED <<run2_client_id, fx_cfg, pos_sell, pos_buy, cash_avail, sell_orders, buy_orders, fx_orders, next_oid, disk_completed, batch_sell_qty, batch_buy_qty, capped_buy_qty, phase_b_intent, phase_a_status, phase_b_status, run1_scaled_buy, fx_converted_total>>

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
    /\ \A x \in disk_completed :
        /\ x.kind \in {"SELL", "FX", "BUY", "BUY_CAP"}
        /\ x.orig >= 0
        /\ x.scaled >= 0

\* 1. NoDoubleOrderAcrossClientIds (Fix L3-A):
\*    Cumulative executed + open working shares across ALL TWS clientIds never exceed planned targets.
NoDoubleOrderAcrossClientIds ==
    /\ (InitSellQty - pos_sell) + TotalWorkingSell <= InitSellQty
    /\ pos_buy + TotalWorkingBuy <= TargetBuyQty

\* 2. NoStrandedUnfilledTargetOnRetry (Fix L3-B):
\*    After Run 2 completes (without FX deduction or partial-sell scaling), no cancelled/expired
\*    WORKING/PARTIAL intent from Run 1 leaves the portfolio stranded with 0 working orders and
\*    unfilled target shares.
NoStrandedUnfilledTargetOnRetry ==
    (step = "RUN2_DONE" /\ fx_cfg = "NO_FX" /\ ~run1_scaled_buy) =>
        /\ (TotalWorkingSell = 0 => pos_sell = 0)
        /\ ((pos_sell = 0 /\ TotalWorkingBuy = 0) => pos_buy = TargetBuyQty)

\* 3. NoScaledBuyPermanentUnderfill (Fix L3-C):
\*    If Run 1 down-scaled Phase B buy due to partial Phase A sell proceeds, and Phase A finishes
\*    filling before Run 2 (`pos_sell = 0`), Run 2 reaches the full `TargetBuyQty` once buys fill.
NoScaledBuyPermanentUnderfill ==
    (step = "RUN2_DONE" /\ fx_cfg = "NO_FX" /\ run1_scaled_buy /\ pos_sell = 0 /\ TotalWorkingBuy = 0)
        => (pos_buy = TargetBuyQty)

\* 4. NoDuplicateFxConversionOnRetry (Fix L3-D):
\*    Retrying `execute --bootstrap-fx-usd B --allow-fx-convert` for the same session never
\*    commits or converts more than `BootstrapFxUsd` cumulative USD across filled + open FX orders.
NoDuplicateFxConversionOnRetry ==
    /\ (fx_cfg = "NO_FX"   => (fx_converted_total = 0 /\ WorkingFxUsd = 0))
    /\ (fx_cfg = "WITH_FX" => (fx_converted_total + WorkingFxUsd <= BootstrapFxUsd))
=============================================================================
