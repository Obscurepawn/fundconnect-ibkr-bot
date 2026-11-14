--------------------- MODULE L1_TwsWalkLimitAndRaces_v2 ---------------------
EXTENDS Integers, FiniteSets, TLC

\* ============================ var <-> code map ============================
\* CONSTANTS (read from src/fundconnect_bot/config.py:213-248)
\*   Sides             - {"BUY", "SELL"} order side domain     (src/fundconnect_bot/execution/policy.py:1029)
\*   ArrivalMidCents   - spec.arrival_mid in cents (10000=$100)(src/fundconnect_bot/execution/policy.py:550)
\*   ReqQty            - spec.quantity (200 shares)            (src/fundconnect_bot/execution/policy.py:547)
\*   LotSize           - spec.lot_size (100 shares)            (src/fundconnect_bot/execution/policy.py:548)
\*   MaxWalkSteps      - config.max_walk_steps (3)             (src/fundconnect_bot/config.py:238)
\*   LiveQuoteSet      - live quote regimes returned in walk   (src/fundconnect_bot/execution/broker.py:391-413)
\*
\* VARIABLES
\*   pc                - control point in _execute_orders_with_walk / broker (src/fundconnect_bot/execution/policy.py:1029-1063)
\*                       DENOTES: {"IDLE","PLACE_SLEEP","POLL_SLEEP","QUOTE_FETCH","CALL_MODIFY","MOD_WIRE_SLEEP","DONE"}
\*   caller_mode       - {"WALK_LOOP", "DIRECT_MODIFY"}        (src/fundconnect_bot/execution/policy.py:1020, broker.py:481)
\*                       DENOTES: _execute_orders_with_walk vs direct BrokerAdapter.modify_order_limit caller
\*   side              - spec.side ("BUY" or "SELL")           (src/fundconnect_bot/execution/policy.py:546)
\*                       DENOTES: immutable order direction for this execution
\*   tick_step         - minimum price variation in cents      (src/fundconnect_bot/execution/hkex_rules.py:87-178)
\*                       DENOTES: 1c (1 bps fine tick) vs 16c (16 bps coarse HKEX tick > 6 bps walk_step_bps)
\*   all_tight_quotes  - witness flag for CoarseTickWalkReachesNextTick (src/fundconnect_bot/execution/policy.py:1049-1056)
\*                       DENOTES: TRUE iff every live quote sampled in QUOTE_FETCH stayed in TIGHT_MOVE
\*   walk_step         - loop variable `step` in 1..MaxWalkSteps (src/fundconnect_bot/execution/policy.py:1036)
\*                       DENOTES: current Peg-to-Mid walk iteration
\*   cur_spec_lmt      - cur_spec.limit_price (cents)          (src/fundconnect_bot/execution/policy.py:1035, 1061)
\*                       DENOTES: limit price tracked on Python OrderSpec
\*   pending_lmt       - new_limit_price passed to modify      (src/fundconnect_bot/execution/policy.py:1050, 1058)
\*                       DENOTES: candidate walk limit (0 when None)
\*   prev_lmt_px       - prev_lmt snapshot in modify_order_limit (src/fundconnect_bot/execution/broker.py:494)
\*                       DENOTES: saved trade.order.lmtPrice restored on Error 104 (broker.py:526)
\*   trade_lmt_px      - trade.order.lmtPrice in ib_async      (src/fundconnect_bot/execution/broker.py:452, 513, 526)
\*                       DENOTES: client-side Trade.order.lmtPrice
\*   venue_lmt_px      - active/executed limit price at IBKR   (src/fundconnect_bot/execution/broker.py:475, 514)
\*                       DENOTES: authoritative broker/exchange order limit price
\*   submitted_oids    - set of TWS orderIds transmitted       (src/fundconnect_bot/execution/broker.py:475, 514)
\*                       DENOTES: witness set for NoDuplicateOrderOnModify
\*   sent_limits       - set of all limit prices sent to TWS   (src/fundconnect_bot/execution/broker.py:475, 514)
\*                       DENOTES: witness set for LimitNeverExceedsSlippageCeiling
\*   tws_status        - trade.orderStatus.status in ib_async  (src/fundconnect_bot/execution/broker.py:435, 486)
\*                       DENOTES: {"NONE","PendingSubmit","PreSubmitted","Submitted","Filled","Inactive","ApiCancelled"}
\*   tws_filled        - trade.orderStatus.filled shares       (src/fundconnect_bot/execution/broker.py:433, 487)
\*                       DENOTES: cumulative executed shares delivered via execDetails/orderStatus
\*   reprice_cnt       - _trades_by_id[order_id][2] counter    (src/fundconnect_bot/execution/broker.py:478, 527, 531-532)
\*                       DENOTES: count of venue-accepted modify_order_limit calls
\*   last_tws_error    - saw_err_104 / TWS error code (0/104/110) (src/fundconnect_bot/execution/broker.py:496-525)
\*                       DENOTES: broker error code observed during current call
\*   mod_wire_resolved - whether in-flight modify settled      (src/fundconnect_bot/execution/broker.py:514-515)
\*                       DENOTES: wire synchronization flag during ib.sleep(0.5) in modify_order_limit
\*   rep_snapshot      - local variable `rep` in walk loop     (src/fundconnect_bot/execution/policy.py:1030, 1042, 1046, 1058)
\*                       DENOTES: most recent FillReport held by _execute_orders_with_walk
\*   final_report      - FillReport returned to caller         (src/fundconnect_bot/execution/policy.py:1032, 1062)
\*                       DENOTES: returned FillReport consumed by execute_market_rebalance
\* =========================================================================

CONSTANTS
    Sides,
    ArrivalMidCents,
    ReqQty,
    LotSize,
    MaxWalkSteps,
    LiveQuoteSet

VARIABLES
    pc,
    caller_mode,
    side,
    tick_step,
    all_tight_quotes,
    walk_step,
    cur_spec_lmt,
    pending_lmt,
    prev_lmt_px,
    trade_lmt_px,
    venue_lmt_px,
    submitted_oids,
    sent_limits,
    tws_status,
    tws_filled,
    reprice_cnt,
    last_tws_error,
    mod_wire_resolved,
    rep_snapshot,
    final_report

vars == <<
    pc, caller_mode, side, tick_step, all_tight_quotes, walk_step,
    cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px,
    submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt,
    last_tws_error, mod_wire_resolved, rep_snapshot, final_report
>>

DoneStates == {"Filled", "Inactive", "ApiCancelled"}
ActiveStates == {"PendingSubmit", "PreSubmitted", "Submitted"}

\* Windows where ib_async runs its asyncio socket loop and processes TWS callbacks:
\*   PLACE_SLEEP    : broker.py:476 (ib.sleep(0.5) inside place_order)
\*   POLL_SLEEP     : broker.py:537-538 (ib.sleep(wait_sec) inside poll_order)
\*   QUOTE_FETCH    : broker.py:396-397 (ib.qualifyContracts / ib.reqTickers inside get_quotes)
\*   MOD_WIRE_SLEEP : broker.py:515 (ib.sleep(0.5) inside modify_order_limit)
AsyncPumpWindows == {"PLACE_SLEEP", "POLL_SLEEP", "QUOTE_FETCH"}

CeilDiv(a, b) == (a + b - 1) \div b
Min2(a, b) == IF a < b THEN a ELSE b
Max2(a, b) == IF a > b THEN a ELSE b
Min3(a, b, c) == Min2(Min2(a, b), c)
Max3(a, b, c) == Max2(Max2(a, b), c)

\* Directional tick rounding matching round_market_price (src/fundconnect_bot/execution/hkex_rules.py:127, 159, 167-178)
RoundToTick(px, s, tstep) ==
    IF s = "BUY"
    THEN (px \div tstep) * tstep
    ELSE CeilDiv(px, tstep) * tstep

\* Initial marketable limit (+8 bps BUY / -8 bps SELL, rounded to tick; src/fundconnect_bot/execution/policy.py:221-230)
InitialLimitCents(s, tstep) ==
    LET raw == IF s = "BUY"
               THEN (ArrivalMidCents * 10008) \div 10000
               ELSE CeilDiv(ArrivalMidCents * 9992, 10000)
    IN RoundToTick(raw, s, tstep)

HardSlippageBound(s) ==
    \* +/- 25 bps ceiling/floor around arrival_mid (src/fundconnect_bot/execution/policy.py:261, 270)
    IF s = "BUY"
    THEN (ArrivalMidCents * 10025) \div 10000
    ELSE CeilDiv(ArrivalMidCents * 9975, 10000)

DirectTargetLimit(s, tstep) ==
    RoundToTick(HardSlippageBound(s), s, tstep)

\* _trade_to_report transcription (src/fundconnect_bot/execution/broker.py:430-455)
TradeToReport(t_status, t_filled, t_lmt, r_cnt) ==
    LET st == IF t_filled >= ReqQty \/ t_status = "Filled" THEN "FILLED"
              ELSE IF t_status \in {"Inactive", "ApiCancelled"} THEN "CANCELLED"
              ELSE IF t_filled > 0 THEN "PARTIAL"
              ELSE "WORKING"
    IN [
        status            |-> st,
        filled_qty        |-> t_filled,
        final_limit_cents |-> t_lmt,
        reprice_count     |-> r_cnt
    ]

EmptyReport == [status |-> "NONE", filled_qty |-> 0, final_limit_cents |-> 0, reprice_count |-> 0]

\* compute_walked_limit_price transcription (src/fundconnect_bot/execution/policy.py:233-278)
ComputeWalkedLimit(s, cur_lmt, q_reg, step_num, tstep) ==
    IF step_num < 1 \/ step_num > MaxWalkSteps THEN 0
    ELSE IF q_reg \in {"HALTED", "WIDE_SPREAD"} THEN 0
    ELSE LET bps == Min2(25, 8 + step_num * 6)
         IN IF s = "BUY" THEN
             LET ask_px   == (ArrivalMidCents * 10030) \div 10000
                 step_cap == (ArrivalMidCents * (10000 + bps)) \div 10000
                 hard_cap == HardSlippageBound("BUY")
                 raw_new  == Min3(ask_px, step_cap, hard_cap)
                 rounded  == RoundToTick(raw_new, "BUY", tstep)
             IN IF rounded > cur_lmt THEN rounded ELSE 0
            ELSE
             LET bid_px   == CeilDiv(ArrivalMidCents * 9970, 10000)
                 step_flr == CeilDiv(ArrivalMidCents * (10000 - bps), 10000)
                 hard_flr == HardSlippageBound("SELL")
                 raw_new  == Max3(bid_px, step_flr, hard_flr)
                 rounded  == RoundToTick(raw_new, "SELL", tstep)
             IN IF rounded < cur_lmt THEN rounded ELSE 0

\* Lookahead coarse-tick check (src/fundconnect_bot/execution/policy.py:1052-1055)
CanAdvanceLater(s, cur_lmt, q_reg, step_num, tstep) ==
    \E later_step \in (step_num + 1)..MaxWalkSteps :
        ComputeWalkedLimit(s, cur_lmt, q_reg, later_step, tstep) /= 0

\* ---- Initial state ----
Init ==
    /\ pc                = "IDLE"
    /\ caller_mode       \in {"WALK_LOOP", "DIRECT_MODIFY"}
    /\ side              \in Sides
    /\ tick_step         \in {1, 16}
    /\ all_tight_quotes  = TRUE
    /\ walk_step         = 0
    /\ cur_spec_lmt      = InitialLimitCents(side, tick_step)
    /\ pending_lmt       = 0
    /\ prev_lmt_px       = 0
    /\ trade_lmt_px      = 0
    /\ venue_lmt_px      = 0
    /\ submitted_oids    = {}
    /\ sent_limits       = {}
    /\ tws_status        = "NONE"
    /\ tws_filled        = 0
    /\ reprice_cnt       = 0
    /\ last_tws_error    = 0
    /\ mod_wire_resolved = FALSE
    /\ rep_snapshot      = EmptyReport
    /\ final_report      = EmptyReport

\* ---- Bot transitions (src/fundconnect_bot/execution/broker.py & policy.py) ----

BotPlaceOrder ==
    \* place_order: ib.placeOrder + enter ib.sleep(0.5) (src/fundconnect_bot/execution/broker.py:457-476)
    /\ pc = "IDLE"
    /\ LET init_px == cur_spec_lmt IN
       /\ trade_lmt_px'   = init_px
       /\ venue_lmt_px'   = init_px
       /\ prev_lmt_px'    = init_px
       /\ submitted_oids' = submitted_oids \cup {1}
       /\ sent_limits'    = sent_limits \cup {init_px}
       /\ tws_status'     = "PendingSubmit"
       /\ pc'             = "PLACE_SLEEP"
       /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

BotFinishPlace ==
    \* place_order return + initial terminal check (src/fundconnect_bot/execution/broker.py:477-479, src/fundconnect_bot/execution/policy.py:1030-1036)
    /\ pc = "PLACE_SLEEP"
    /\ LET rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, 0) IN
       /\ rep_snapshot' = rep
       /\ IF rep.status \in {"FILLED", "CANCELLED"}
          THEN /\ final_report' = rep
               /\ pc'           = "DONE"
               /\ UNCHANGED walk_step
          ELSE /\ walk_step'    = 1
               /\ pc'           = "POLL_SLEEP"
               /\ UNCHANGED final_report
       /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotFinishPoll ==
    \* poll_order return: WALK_LOOP checks terminal status before get_quotes (policy.py:1037-1045);
    \* DIRECT_MODIFY invokes modify_order_limit directly to exercise broker.py:486-492 DoneStates guard.
    /\ pc = "POLL_SLEEP"
    /\ LET rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ rep_snapshot' = rep
       /\ IF caller_mode = "DIRECT_MODIFY"
          THEN /\ pending_lmt' = DirectTargetLimit(side, tick_step)
               /\ pc'          = "CALL_MODIFY"
               /\ UNCHANGED final_report
          ELSE IF rep.status \in {"FILLED", "CANCELLED"}
               THEN /\ final_report' = rep
                    /\ pc'           = "DONE"
                    /\ UNCHANGED pending_lmt
               ELSE /\ pc'           = "QUOTE_FETCH"
                    /\ UNCHANGED <<pending_lmt, final_report>>
       /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotPostQuoteTerminalBreak ==
    \* Fix L1-B: after get_quotes() pumps asyncio, poll_order(wait_sec=0.0) refreshes `rep` and breaks
    \* immediately if FILLED or CANCELLED (src/fundconnect_bot/execution/policy.py:1045-1048).
    /\ pc = "QUOTE_FETCH"
    /\ LET fresh_rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ fresh_rep.status \in {"FILLED", "CANCELLED"}
       /\ rep_snapshot' = fresh_rep
       /\ final_report' = fresh_rep
       /\ pc'           = "DONE"
       /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotCoarseTickContinue ==
    \* Fix M1: when current step rounds back to cur_spec.limit_price (`new_lmt = 0`) on a coarse HKEX tick
    \* but a later step in `step+1..MaxWalkSteps` advances at the same quote, continue walking!
    \* (src/fundconnect_bot/execution/policy.py:1049-1056)
    /\ pc = "QUOTE_FETCH"
    /\ LET fresh_rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ fresh_rep.status \notin {"FILLED", "CANCELLED"}
       /\ \E q_reg \in LiveQuoteSet :
            /\ ComputeWalkedLimit(side, cur_spec_lmt, q_reg, walk_step, tick_step) = 0
            /\ CanAdvanceLater(side, cur_spec_lmt, q_reg, walk_step, tick_step)
            /\ all_tight_quotes' = (all_tight_quotes /\ (q_reg = "TIGHT_MOVE"))
            /\ rep_snapshot'     = fresh_rep
            /\ walk_step'        = walk_step + 1
            /\ pc'               = "POLL_SLEEP"
    /\ UNCHANGED <<caller_mode, side, tick_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, final_report>>

BotWalkNoAdvanceBreak ==
    \* When new_lmt is None and no later step can advance (halted, wide spread, or at slippage cap),
    \* exit walk loop cleanly with refreshed `rep` (src/fundconnect_bot/execution/policy.py:1046-1057, 1062).
    /\ pc = "QUOTE_FETCH"
    /\ LET fresh_rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ fresh_rep.status \notin {"FILLED", "CANCELLED"}
       /\ \E q_reg \in LiveQuoteSet :
            /\ ComputeWalkedLimit(side, cur_spec_lmt, q_reg, walk_step, tick_step) = 0
            /\ ~CanAdvanceLater(side, cur_spec_lmt, q_reg, walk_step, tick_step)
            /\ all_tight_quotes' = (all_tight_quotes /\ (q_reg = "TIGHT_MOVE"))
            /\ rep_snapshot'     = fresh_rep
            /\ final_report'     = fresh_rep
            /\ pc'               = "DONE"
    /\ UNCHANGED <<caller_mode, side, tick_step, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotWalkScheduleModify ==
    \* Positive walk step advances limit -> call broker.modify_order_limit (src/fundconnect_bot/execution/policy.py:1050, 1058)
    /\ pc = "QUOTE_FETCH"
    /\ LET fresh_rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ fresh_rep.status \notin {"FILLED", "CANCELLED"}
       /\ \E q_reg \in LiveQuoteSet :
            LET new_lmt == ComputeWalkedLimit(side, cur_spec_lmt, q_reg, walk_step, tick_step) IN
            /\ new_lmt /= 0
            /\ all_tight_quotes' = (all_tight_quotes /\ (q_reg = "TIGHT_MOVE"))
            /\ rep_snapshot'     = fresh_rep
            /\ pending_lmt'      = new_lmt
            /\ pc'               = "CALL_MODIFY"
    /\ UNCHANGED <<caller_mode, side, tick_step, walk_step, cur_spec_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, final_report>>

BotModifyShortCircuitDone ==
    \* Fix L1-A: modify_order_limit checks DoneStates or filled_now >= spec.quantity BEFORE mutating
    \* trade.order.lmtPrice or calling ib.placeOrder (src/fundconnect_bot/execution/broker.py:486-492).
    /\ pc = "CALL_MODIFY"
    /\ (tws_status \in DoneStates \/ tws_filled >= ReqQty)
    /\ LET rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ rep_snapshot' = rep
       /\ final_report' = rep
       /\ pc'           = "DONE"
       /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotModifyTransmit ==
    \* modify_order_limit snapshots prev_lmt, subscribes _on_error for Error 104, and transmits updated
    \* lmtPrice on existing orderId=1 (src/fundconnect_bot/execution/broker.py:494-515).
    /\ pc = "CALL_MODIFY"
    /\ tws_status \in ActiveStates
    /\ tws_filled < ReqQty
    /\ prev_lmt_px'       = trade_lmt_px
    /\ trade_lmt_px'      = pending_lmt
    /\ submitted_oids'    = submitted_oids \cup {1}
    /\ sent_limits'       = sent_limits \cup {pending_lmt}
    /\ last_tws_error'    = 0
    /\ mod_wire_resolved' = FALSE
    /\ pc'                = "MOD_WIRE_SLEEP"
    /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, venue_lmt_px, tws_status, tws_filled, reprice_cnt, rep_snapshot, final_report>>

BotFinishModifyError104 ==
    \* Fix L1-C: on Error 104 ("Can't modify a filled order"), restore trade.order.lmtPrice = prev_lmt,
    \* keep (prev_spec, prev_cnt), and return _trade_to_report (src/fundconnect_bot/execution/broker.py:520-528, policy.py:1058-1060).
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = TRUE
    /\ last_tws_error = 104
    /\ LET rep == TradeToReport(tws_status, tws_filled, prev_lmt_px, reprice_cnt) IN
       /\ trade_lmt_px' = prev_lmt_px
       /\ rep_snapshot' = rep
       /\ final_report' = rep
       /\ pc'           = "DONE"
       /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotFinishModifySuccess ==
    \* Accepted modification updates updated_spec, increments cnt = prev_cnt + 1, and updates cur_spec
    \* (src/fundconnect_bot/execution/broker.py:530-533, src/fundconnect_bot/execution/policy.py:1058-1062).
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = TRUE
    /\ last_tws_error /= 104
    /\ LET new_cnt == reprice_cnt + 1
           rep     == TradeToReport(tws_status, tws_filled, trade_lmt_px, new_cnt)
       IN /\ reprice_cnt'  = new_cnt
          /\ cur_spec_lmt' = trade_lmt_px
          /\ rep_snapshot' = rep
          /\ IF caller_mode = "DIRECT_MODIFY" \/ rep.status \in {"FILLED", "CANCELLED"} \/ walk_step >= MaxWalkSteps
             THEN /\ final_report' = rep
                  /\ pc'           = "DONE"
                  /\ UNCHANGED walk_step
             ELSE /\ walk_step'    = walk_step + 1
                  /\ pc'           = "POLL_SLEEP"
                  /\ UNCHANGED final_report
          /\ UNCHANGED <<caller_mode, side, tick_step, all_tight_quotes, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, last_tws_error, mod_wire_resolved>>

\* ---- Asynchronous TWS / venue transitions during ib_async event-loop pumps ----

TwsAckOrder ==
    \* PendingSubmit -> PreSubmitted -> Submitted (src/fundconnect_bot/execution/broker.py:476, 538, 396-397)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PendingSubmit", "PreSubmitted"}
    /\ tws_status' = IF tws_status = "PendingSubmit" THEN "PreSubmitted" ELSE "Submitted"
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsRejectInactive ==
    \* Error 110 (tick size) or Error 201 (margin/permission) -> Inactive (src/fundconnect_bot/execution/broker.py:438-439)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in ActiveStates
    /\ tws_filled = 0
    /\ tws_status'     = "Inactive"
    /\ last_tws_error' = 110
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, mod_wire_resolved, rep_snapshot, final_report>>

TwsPartialFill ==
    \* Partial venue execution (LotSize < ReqQty) delivered via execDetails + orderStatus (src/fundconnect_bot/execution/broker.py:433, 440-441)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_filled = 0
    /\ LotSize < ReqQty
    /\ tws_filled' = LotSize
    /\ tws_status' = "Submitted"
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsFullFill ==
    \* Full venue execution -> status = "Filled" (src/fundconnect_bot/execution/broker.py:436-437)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_filled < ReqQty
    /\ tws_filled' = ReqQty
    /\ tws_status' = "Filled"
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsAsyncCancel ==
    \* Unsolicited TWS / supervisory cancel -> status = "ApiCancelled" (src/fundconnect_bot/execution/broker.py:438-439)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_status' = "ApiCancelled"
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsModifyAccepted ==
    \* Venue accepts in-place orderId=1 limit modification (src/fundconnect_bot/execution/broker.py:514-515)
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = FALSE
    /\ venue_lmt_px'      = trade_lmt_px
    /\ tws_status'        = "Submitted"
    /\ mod_wire_resolved' = TRUE
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, last_tws_error, rep_snapshot, final_report>>

TwsError104WireRace ==
    \* Server-side wire race: order matched in full at prior venue_lmt_px before modify arrived;
    \* TWS emits Error 104 + orderStatus("Filled") while rejecting new limit on venue (src/fundconnect_bot/execution/broker.py:499-528)
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = FALSE
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_filled'        = ReqQty
    /\ tws_status'        = "Filled"
    /\ last_tws_error'    = 104
    /\ mod_wire_resolved' = TRUE
    /\ UNCHANGED <<pc, caller_mode, side, tick_step, all_tight_quotes, walk_step, cur_spec_lmt, pending_lmt, prev_lmt_px, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, reprice_cnt, rep_snapshot, final_report>>

TerminalStutter ==
    \* Clean completion (src/fundconnect_bot/execution/policy.py:1062-1063)
    /\ pc = "DONE"
    /\ UNCHANGED vars

Next ==
    \/ BotPlaceOrder
    \/ BotFinishPlace
    \/ BotFinishPoll
    \/ BotPostQuoteTerminalBreak
    \/ BotCoarseTickContinue
    \/ BotWalkNoAdvanceBreak
    \/ BotWalkScheduleModify
    \/ BotModifyShortCircuitDone
    \/ BotModifyTransmit
    \/ BotFinishModifyError104
    \/ BotFinishModifySuccess
    \/ TwsAckOrder
    \/ TwsRejectInactive
    \/ TwsPartialFill
    \/ TwsFullFill
    \/ TwsAsyncCancel
    \/ TwsModifyAccepted
    \/ TwsError104WireRace
    \/ TerminalStutter

Spec == Init /\ [][Next]_vars

\* =========================================================================
\* Developer-Expectation Invariants
\* =========================================================================

TypeOK ==
    /\ pc \in {"IDLE", "PLACE_SLEEP", "POLL_SLEEP", "QUOTE_FETCH", "CALL_MODIFY", "MOD_WIRE_SLEEP", "DONE"}
    /\ caller_mode \in {"WALK_LOOP", "DIRECT_MODIFY"}
    /\ side \in Sides
    /\ tick_step \in {1, 16}
    /\ all_tight_quotes \in BOOLEAN
    /\ walk_step \in 0..MaxWalkSteps
    /\ tws_status \in ({"NONE"} \cup ActiveStates \cup DoneStates)
    /\ tws_filled \in 0..ReqQty
    /\ reprice_cnt \in 0..MaxWalkSteps

\* 1. LimitNeverExceedsSlippageCeiling:
\*    Every limit price ever transmitted to TWS stays within +/- max_slippage_bps (25 bps) of arrival_mid
\*    and sits on an exact market tick multiple.
LimitNeverExceedsSlippageCeiling ==
    \A px \in sent_limits :
        /\ px > 0
        /\ px % tick_step = 0
        /\ (side = "BUY"  => px <= HardSlippageBound("BUY"))
        /\ (side = "SELL" => px >= HardSlippageBound("SELL"))

\* 2. NoDuplicateOrderOnModify:
\*    Repricing modifies the existing orderId in-place; at most 1 orderId is ever submitted for the OrderSpec.
NoDuplicateOrderOnModify ==
    Cardinality(submitted_oids) <= 1

\* 3. NoModifyOnTerminalOrder (Fix L1-A):
\*    modify_order_limit never calls ib.placeOrder on a Trade already in OrderStatus.DoneStates
\*    and never mutates trade.order.lmtPrice away from venue_lmt_px when short-circuiting a DoneState.
NoModifyOnTerminalOrder ==
    /\ pc /= "CRASHED_ASSERT_DONE"
    /\ ((pc = "MOD_WIRE_SLEEP" /\ mod_wire_resolved = FALSE) => tws_status \in ActiveStates)

\* 4. FillAccountingExact (Fixes L1-B & L1-C):
\*    Whenever execution completes (pc = "DONE"), final_report accurately reflects the broker Trade's
\*    cumulative filled_qty, terminal vs working status, and venue-accepted limit price (rolling back
\*    rejected Error 104 modifications).
FillAccountingExact ==
    (pc = "DONE") =>
        /\ final_report.filled_qty = tws_filled
        /\ tws_filled <= ReqQty
        /\ (tws_status = "Filled" => final_report.status = "FILLED")
        /\ ((tws_status \in {"Inactive", "ApiCancelled"} /\ tws_filled < ReqQty)
                => final_report.status = "CANCELLED")
        /\ final_report.final_limit_cents = venue_lmt_px
        /\ trade_lmt_px = venue_lmt_px

\* 5. CoarseTickWalkReachesNextTick (Fix M1):
\*    Under continuous TIGHT_MOVE quotes in _execute_orders_with_walk, an order that stays WORKING/PARTIAL
\*    after exhausting MaxWalkSteps reaches the furthest tick-quantized limit within HardSlippageBound
\*    even when 1 tick (16 bps) exceeds walk_step_bps (6 bps).
CoarseTickWalkReachesNextTick ==
    (caller_mode = "WALK_LOOP" /\ pc = "DONE" /\ final_report.status \in {"WORKING", "PARTIAL"} /\ all_tight_quotes)
        => /\ cur_spec_lmt = RoundToTick(HardSlippageBound(side), side, tick_step)
           /\ final_report.final_limit_cents = RoundToTick(HardSlippageBound(side), side, tick_step)
=============================================================================
