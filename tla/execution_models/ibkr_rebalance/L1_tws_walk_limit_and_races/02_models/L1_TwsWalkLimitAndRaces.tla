---------------------- MODULE L1_TwsWalkLimitAndRaces ----------------------
EXTENDS Integers, FiniteSets, TLC

\* ============================ var <-> code map ============================
\* CONSTANTS (read from src/fundconnect_bot/config.py:209-239)
\*   Sides             - {"BUY", "SELL"} order side domain     (src/fundconnect_bot/execution/policy.py:1020)
\*   ArrivalMidCents   - spec.arrival_mid in cents (10000=$100)(src/fundconnect_bot/execution/policy.py:550)
\*   ReqQty            - spec.quantity (200 shares)            (src/fundconnect_bot/execution/policy.py:547)
\*   LotSize           - spec.lot_size (100 shares)            (src/fundconnect_bot/execution/policy.py:548)
\*   MaxWalkSteps      - config.max_walk_steps (3)             (src/fundconnect_bot/config.py:224)
\*   LiveQuoteSet      - live quote regimes returned in walk   (src/fundconnect_bot/execution/broker.py:371-393)
\*
\* VARIABLES
\*   pc                - control point in _execute_orders_with_walk / broker (src/fundconnect_bot/execution/policy.py:1020-1046)
\*                       DENOTES: {"IDLE","PLACE_SLEEP","POLL_SLEEP","QUOTE_FETCH","CALL_MODIFY","MOD_WIRE_SLEEP","DONE","CRASHED_ASSERT_DONE"}
\*   side              - spec.side ("BUY" or "SELL")           (src/fundconnect_bot/execution/policy.py:546)
\*                       DENOTES: immutable order direction for this execution
\*   walk_step         - loop variable `step` in 1..MaxWalkSteps (src/fundconnect_bot/execution/policy.py:1027)
\*                       DENOTES: current Peg-to-Mid walk iteration
\*   cur_spec_lmt      - cur_spec.limit_price (cents)          (src/fundconnect_bot/execution/policy.py:1026, 1043)
\*                       DENOTES: limit price tracked on Python OrderSpec
\*   pending_lmt       - new_lmt returned by compute_walked_limit_price (src/fundconnect_bot/execution/policy.py:1037-1042)
\*                       DENOTES: candidate walk limit (0 when None)
\*   trade_lmt_px      - trade.order.lmtPrice in ib_async      (src/fundconnect_bot/execution/broker.py:454, 467)
\*                       DENOTES: client-side Trade.order.lmtPrice (mutated in-place at broker.py:467)
\*   venue_lmt_px      - active/executed limit price at IBKR   (src/fundconnect_bot/execution/broker.py:455, 468)
\*                       DENOTES: authoritative broker/exchange order limit price
\*   submitted_oids    - set of TWS orderIds transmitted       (src/fundconnect_bot/execution/broker.py:455, 468)
\*                       DENOTES: witness set for NoDuplicateOrderOnModify
\*   sent_limits       - set of all limit prices sent to TWS   (src/fundconnect_bot/execution/broker.py:455, 468)
\*                       DENOTES: witness set for LimitNeverExceedsSlippageCeiling
\*   tws_status        - trade.orderStatus.status in ib_async  (src/fundconnect_bot/execution/broker.py:415)
\*                       DENOTES: {"NONE","PendingSubmit","PreSubmitted","Submitted","Filled","Inactive","ApiCancelled"}
\*   tws_filled        - trade.orderStatus.filled shares       (src/fundconnect_bot/execution/broker.py:413)
\*                       DENOTES: cumulative executed shares delivered to client via execDetails/orderStatus
\*   reprice_cnt       - _trades_by_id[order_id][2] counter    (src/fundconnect_bot/execution/broker.py:458, 470-471)
\*                       DENOTES: count of completed modify_order_limit calls
\*   last_tws_error    - most recent TWS error code (0/104/110)(src/fundconnect_bot/execution/broker.py:468-469)
\*                       DENOTES: broker error code observed on trade (104=modify filled, 110/201=reject)
\*   mod_wire_resolved - whether in-flight modify settled      (src/fundconnect_bot/execution/broker.py:468-469)
\*                       DENOTES: wire synchronization flag during ib.sleep(0.5) in modify_order_limit
\*   rep_snapshot      - local variable `rep` in walk loop     (src/fundconnect_bot/execution/policy.py:1021, 1033, 1042)
\*                       DENOTES: most recent FillReport held by _execute_orders_with_walk
\*   final_report      - FillReport appended to `reports`      (src/fundconnect_bot/execution/policy.py:1023, 1046)
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
    side,
    walk_step,
    cur_spec_lmt,
    pending_lmt,
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
    pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px,
    venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled,
    reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report
>>

DoneStates == {"Filled", "Inactive", "ApiCancelled"}
ActiveStates == {"PendingSubmit", "PreSubmitted", "Submitted"}

\* Windows where ib_async runs its asyncio socket loop and processes TWS callbacks:
\*   PLACE_SLEEP    : broker.py:456 (ib.sleep(0.5) inside place_order)
\*   POLL_SLEEP     : broker.py:477 (ib.sleep(wait_sec) inside poll_order)
\*   QUOTE_FETCH    : broker.py:376-377 (ib.qualifyContracts / ib.reqTickers inside get_quotes)
\*   MOD_WIRE_SLEEP : broker.py:469 (ib.sleep(0.5) inside modify_order_limit)
AsyncPumpWindows == {"PLACE_SLEEP", "POLL_SLEEP", "QUOTE_FETCH"}

CeilDiv(a, b) == (a + b - 1) \div b
Min2(a, b) == IF a < b THEN a ELSE b
Max2(a, b) == IF a > b THEN a ELSE b
Min3(a, b, c) == Min2(Min2(a, b), c)
Max3(a, b, c) == Max2(Max2(a, b), c)

\* Initial marketable limit (+8 bps BUY / -8 bps SELL, src/fundconnect_bot/execution/policy.py:221-230)
InitialLimitCents(s) ==
    IF s = "BUY"
    THEN (ArrivalMidCents * 10008) \div 10000
    ELSE CeilDiv(ArrivalMidCents * 9992, 10000)

HardSlippageBound(s) ==
    \* +/- 25 bps ceiling/floor around arrival_mid (src/fundconnect_bot/execution/policy.py:261, 270)
    IF s = "BUY"
    THEN (ArrivalMidCents * 10025) \div 10000
    ELSE CeilDiv(ArrivalMidCents * 9975, 10000)

\* _trade_to_report transcription (src/fundconnect_bot/execution/broker.py:410-435)
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
\* Live quote regimes:
\*   "TIGHT_MOVE"  : valid quote (14 bps spread) where ask = mid + 30 bps (BUY) / bid = mid - 30 bps (SELL)
\*                   so each walk step (+14, +20, +25 bps) advances monotonically up to the 25 bps cap.
\*   "WIDE_SPREAD" : valid quote with 90 bps spread > max_spread_bps -> returns 0 (None).
\*   "HALTED"      : invalid quote (bid=0, ask=0) -> returns 0 (None).
ComputeWalkedLimit(s, cur_lmt, q_reg, step_num) ==
    IF step_num < 1 \/ step_num > MaxWalkSteps THEN 0
    ELSE IF q_reg \in {"HALTED", "WIDE_SPREAD"} THEN 0
    ELSE LET bps == Min2(25, 8 + step_num * 6)
         IN IF s = "BUY" THEN
             LET ask_px   == (ArrivalMidCents * 10030) \div 10000
                 step_cap == (ArrivalMidCents * (10000 + bps)) \div 10000
                 hard_cap == HardSlippageBound("BUY")
                 raw_new  == Min3(ask_px, step_cap, hard_cap)
             IN IF raw_new > cur_lmt THEN raw_new ELSE 0
            ELSE
             LET bid_px   == CeilDiv(ArrivalMidCents * 9970, 10000)
                 step_flr == CeilDiv(ArrivalMidCents * (10000 - bps), 10000)
                 hard_flr == HardSlippageBound("SELL")
                 raw_new  == Max3(bid_px, step_flr, hard_flr)
             IN IF raw_new < cur_lmt THEN raw_new ELSE 0

\* ---- Initial state ----
Init ==
    /\ pc                = "IDLE"
    /\ side              \in Sides
    /\ walk_step         = 0
    /\ cur_spec_lmt      = InitialLimitCents(side)
    /\ pending_lmt       = 0
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
    \* place_order: ib.placeOrder + enter ib.sleep(0.5) (src/fundconnect_bot/execution/broker.py:437-456)
    /\ pc = "IDLE"
    /\ LET init_px == cur_spec_lmt IN
       /\ trade_lmt_px'   = init_px
       /\ venue_lmt_px'   = init_px
       /\ submitted_oids' = submitted_oids \cup {1}
       /\ sent_limits'    = sent_limits \cup {init_px}
       /\ tws_status'     = "PendingSubmit"
       /\ pc'             = "PLACE_SLEEP"
       /\ UNCHANGED <<side, walk_step, cur_spec_lmt, pending_lmt, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

BotFinishPlace ==
    \* place_order return + initial terminal check (src/fundconnect_bot/execution/broker.py:457-459, src/fundconnect_bot/execution/policy.py:1021-1027)
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
       /\ UNCHANGED <<side, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotFinishPoll ==
    \* poll_order return + terminal check before get_quotes (src/fundconnect_bot/execution/broker.py:474-479, src/fundconnect_bot/execution/policy.py:1033-1036)
    /\ pc = "POLL_SLEEP"
    /\ LET rep == TradeToReport(tws_status, tws_filled, trade_lmt_px, reprice_cnt) IN
       /\ rep_snapshot' = rep
       /\ IF rep.status \in {"FILLED", "CANCELLED"}
          THEN /\ final_report' = rep
               /\ pc'           = "DONE"
          ELSE /\ pc'           = "QUOTE_FETCH"
               /\ UNCHANGED final_report
       /\ UNCHANGED <<side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved>>

BotQuotesAndComputeWalk ==
    \* get_quotes return + compute_walked_limit_price (src/fundconnect_bot/execution/policy.py:1036-1041)
    \* Note: if new_lmt is None (0), policy.py:1040-1041 executes `break` and appends `rep_snapshot`
    \* at policy.py:1046 WITHOUT re-reading trade.orderStatus after get_quotes() pumped asyncio!
    /\ pc = "QUOTE_FETCH"
    /\ \E q_reg \in LiveQuoteSet :
       LET new_lmt == ComputeWalkedLimit(side, cur_spec_lmt, q_reg, walk_step) IN
       IF new_lmt = 0
       THEN /\ final_report' = rep_snapshot
            /\ pc'           = "DONE"
            /\ UNCHANGED pending_lmt
       ELSE /\ pending_lmt'  = new_lmt
            /\ pc'           = "CALL_MODIFY"
            /\ UNCHANGED final_report
    /\ UNCHANGED <<side, walk_step, cur_spec_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot>>

BotModifyCrashOnDoneState ==
    \* modify_order_limit called when trade.orderStatus.status is already in OrderStatus.DoneStates:
    \* broker.py:467 mutates trade.order.lmtPrice and broker.py:468 calls ib.placeOrder, which raises
    \* AssertionError (`assert status not in DoneStates`) in ib_async (src/fundconnect_bot/execution/broker.py:465-468)
    /\ pc = "CALL_MODIFY"
    /\ tws_status \in DoneStates
    /\ trade_lmt_px' = pending_lmt
    /\ pc'           = "CRASHED_ASSERT_DONE"
    /\ UNCHANGED <<side, walk_step, cur_spec_lmt, pending_lmt, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

BotModifyTransmit ==
    \* modify_order_limit transmits updated lmtPrice on existing orderId=1 (src/fundconnect_bot/execution/broker.py:465-469)
    /\ pc = "CALL_MODIFY"
    /\ tws_status \in ActiveStates
    /\ trade_lmt_px'      = pending_lmt
    /\ submitted_oids'    = submitted_oids \cup {1}
    /\ sent_limits'       = sent_limits \cup {pending_lmt}
    /\ mod_wire_resolved' = FALSE
    /\ pc'                = "MOD_WIRE_SLEEP"
    /\ UNCHANGED <<side, walk_step, cur_spec_lmt, pending_lmt, venue_lmt_px, tws_status, tws_filled, reprice_cnt, last_tws_error, rep_snapshot, final_report>>

BotFinishModify ==
    \* modify_order_limit completes ib.sleep(0.5) and returns FillReport (src/fundconnect_bot/execution/broker.py:470-472, src/fundconnect_bot/execution/policy.py:1042-1046)
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = TRUE
    /\ LET new_cnt == reprice_cnt + 1
           rep     == TradeToReport(tws_status, tws_filled, trade_lmt_px, new_cnt)
       IN /\ reprice_cnt'  = new_cnt
          /\ cur_spec_lmt' = pending_lmt
          /\ rep_snapshot' = rep
          /\ IF rep.status \in {"FILLED", "CANCELLED"} \/ walk_step >= MaxWalkSteps
             THEN /\ final_report' = rep
                  /\ pc'           = "DONE"
                  /\ UNCHANGED walk_step
             ELSE /\ walk_step'    = walk_step + 1
                  /\ pc'           = "POLL_SLEEP"
                  /\ UNCHANGED final_report
          /\ UNCHANGED <<side, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_status, tws_filled, last_tws_error, mod_wire_resolved>>

\* ---- Asynchronous TWS / venue transitions during ib_async event-loop pumps ----

TwsAckOrder ==
    \* PendingSubmit -> PreSubmitted -> Submitted (src/fundconnect_bot/execution/broker.py:456, 477, 376-377)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PendingSubmit", "PreSubmitted"}
    /\ tws_status' = IF tws_status = "PendingSubmit" THEN "PreSubmitted" ELSE "Submitted"
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsRejectInactive ==
    \* Error 110 (tick size) or Error 201 (margin/permission) -> Inactive (src/fundconnect_bot/execution/broker.py:418-419)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in ActiveStates
    /\ tws_filled = 0
    /\ tws_status'     = "Inactive"
    /\ last_tws_error' = 110
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, mod_wire_resolved, rep_snapshot, final_report>>

TwsPartialFill ==
    \* Partial venue execution (LotSize < ReqQty) delivered via execDetails + orderStatus (src/fundconnect_bot/execution/broker.py:413, 420-421)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_filled = 0
    /\ LotSize < ReqQty
    /\ tws_filled' = LotSize
    /\ tws_status' = "Submitted"
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsFullFill ==
    \* Full venue execution -> status = "Filled" (src/fundconnect_bot/execution/broker.py:416-417)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_filled < ReqQty
    /\ tws_filled' = ReqQty
    /\ tws_status' = "Filled"
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsAsyncCancel ==
    \* Unsolicited TWS / supervisory cancel -> status = "ApiCancelled" (src/fundconnect_bot/execution/broker.py:418-419)
    /\ pc \in AsyncPumpWindows
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_status' = "ApiCancelled"
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, last_tws_error, mod_wire_resolved, rep_snapshot, final_report>>

TwsModifyAccepted ==
    \* Venue accepts in-place orderId=1 limit modification (src/fundconnect_bot/execution/broker.py:468-469)
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = FALSE
    /\ venue_lmt_px'      = trade_lmt_px
    /\ tws_status'        = "Submitted"
    /\ mod_wire_resolved' = TRUE
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, submitted_oids, sent_limits, tws_filled, reprice_cnt, last_tws_error, rep_snapshot, final_report>>

TwsError104WireRace ==
    \* Server-side wire race: order matched in full at prior venue_lmt_px before modify arrived;
    \* TWS emits Error 104 ("Can't modify a filled order") + orderStatus("Filled") while rejecting
    \* the new limit price on the venue (src/fundconnect_bot/execution/broker.py:467-472)
    /\ pc = "MOD_WIRE_SLEEP"
    /\ mod_wire_resolved = FALSE
    /\ tws_status \in {"PreSubmitted", "Submitted"}
    /\ tws_filled'        = ReqQty
    /\ tws_status'        = "Filled"
    /\ last_tws_error'    = 104
    /\ mod_wire_resolved' = TRUE
    /\ UNCHANGED <<pc, side, walk_step, cur_spec_lmt, pending_lmt, trade_lmt_px, venue_lmt_px, submitted_oids, sent_limits, reprice_cnt, rep_snapshot, final_report>>

TerminalStutter ==
    \* Terminal completion or unhandled AssertionError halt (src/fundconnect_bot/execution/policy.py:1046-1047)
    /\ pc \in {"DONE", "CRASHED_ASSERT_DONE"}
    /\ UNCHANGED vars

Next ==
    \/ BotPlaceOrder
    \/ BotFinishPlace
    \/ BotFinishPoll
    \/ BotQuotesAndComputeWalk
    \/ BotModifyCrashOnDoneState
    \/ BotModifyTransmit
    \/ BotFinishModify
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
    /\ pc \in {"IDLE", "PLACE_SLEEP", "POLL_SLEEP", "QUOTE_FETCH", "CALL_MODIFY", "MOD_WIRE_SLEEP", "DONE", "CRASHED_ASSERT_DONE"}
    /\ side \in Sides
    /\ walk_step \in 0..MaxWalkSteps
    /\ tws_status \in ({"NONE"} \cup ActiveStates \cup DoneStates)
    /\ tws_filled \in 0..ReqQty
    /\ reprice_cnt \in 0..MaxWalkSteps

\* 1. LimitNeverExceedsSlippageCeiling:
\*    Every limit price ever transmitted to TWS stays within +/- max_slippage_bps (25 bps) of arrival_mid.
LimitNeverExceedsSlippageCeiling ==
    \A px \in sent_limits :
        /\ px > 0
        /\ (side = "BUY"  => px <= HardSlippageBound("BUY"))
        /\ (side = "SELL" => px >= HardSlippageBound("SELL"))

\* 2. NoDuplicateOrderOnModify:
\*    Repricing modifies the existing orderId in-place; at most 1 orderId is ever submitted for the OrderSpec.
NoDuplicateOrderOnModify ==
    Cardinality(submitted_oids) <= 1

\* 3. NoModifyOnTerminalOrder:
\*    modify_order_limit never calls ib.placeOrder on a Trade already in OrderStatus.DoneStates
\*    (which raises an unhandled AssertionError in ib_async mid-batch).
NoModifyOnTerminalOrder ==
    pc /= "CRASHED_ASSERT_DONE"

\* 4. FillAccountingExact:
\*    Whenever _execute_orders_with_walk returns normally (pc = "DONE"), final_report accurately reflects
\*    the broker Trade's cumulative filled_qty, terminal vs working status, and venue-accepted limit price.
FillAccountingExact ==
    (pc = "DONE") =>
        /\ final_report.filled_qty = tws_filled
        /\ tws_filled <= ReqQty
        /\ (tws_status = "Filled" => final_report.status = "FILLED")
        /\ ((tws_status \in {"Inactive", "ApiCancelled"} /\ tws_filled < ReqQty)
                => final_report.status = "CANCELLED")
        /\ final_report.final_limit_cents = venue_lmt_px
=============================================================================
