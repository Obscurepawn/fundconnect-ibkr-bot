----------------------- MODULE L0_GapSizingAndHkex_v2 -----------------------
EXTENDS Integers, Sequences, FiniteSets, TLC

\* ============================ var <-> code map ============================
\* CONSTANTS (read from src/fundconnect_bot/config.py:122-240)
\*   LotS1             - board lot for "0002.HK" Part A stock (src/fundconnect_bot/config.py:133)
\*   LotS2             - board lot for "2828.HK" Part D ETF   (src/fundconnect_bot/config.py:124)
\*   InitQtySet        - initial share holdings per non-def   (src/fundconnect_bot/execution/policy.py:618)
\*   BaseBudgetSet     - planned target notional (cents)      (src/fundconnect_bot/execution/policy.py:619-623)
\*   DefBaseBudget     - defensive sleeve base notional       (src/fundconnect_bot/execution/policy.py:827-831)
\*   QuoteRegimes      - quote shock/spread/boundary regimes  (src/fundconnect_bot/execution/policy.py:626-823)
\*   MinNotionalCents  - min_trade_hkd churn threshold        (src/fundconnect_bot/config.py:218)
\*
\* VARIABLES
\*   stage             - pipeline cursor in build_execution_batch (src/fundconnect_bot/execution/policy.py:616-906)
\*                       DENOTES: control-flow progress {S1, S2, DEF, SORT, EXEC, DONE}
\*   cur_qty           - held shares map per symbol           (src/fundconnect_bot/execution/policy.py:310-313)
\*                       DENOTES: point-in-time non-negative integer position snapshot
\*   base_notional     - planned target notional (cents)      (src/fundconnect_bot/execution/policy.py:619-623)
\*                       DENOTES: w_i * total_eq_local upper budget boundary per symbol
\*   q_regime          - live quote scenario per non-def sym  (src/fundconnect_bot/execution/policy.py:624)
\*                       DENOTES: immutable Level-1 Quote snapshot at batch construction
\*   eff_notional      - post-gap effective notional (cents)  (src/fundconnect_bot/execution/policy.py:390)
\*                       DENOTES: gap-adjusted target notional per symbol
\*   sym_redirected    - notional routed to defensive per sym (src/fundconnect_bot/execution/policy.py:391)
\*                       DENOTES: unspent incremental buy budget redirected to 3053.HK
\*   redirected_def    - cumulative redirected_defensive      (src/fundconnect_bot/execution/policy.py:332)
\*                       DENOTES: accumulator added to def_base at step 2 (policy.py:832)
\*   decisions         - recorded InstrumentDecision map      (src/fundconnect_bot/execution/policy.py:328)
\*                       DENOTES: per-symbol ActionCode + desired_total_qty audit record
\*   phase_a_sells     - emitted Phase A sell OrderSpec seq   (src/fundconnect_bot/execution/policy.py:329)
\*                       DENOTES: sell batch sorted by (symbol, slice_index)
\*   phase_b_buys      - emitted Phase B buy OrderSpec seq    (src/fundconnect_bot/execution/policy.py:330)
\*                       DENOTES: buy batch sorted by (symbol == def, symbol, slice_index)
\*   exec_queue        - concatenated Phase A -> Phase B seq  (src/fundconnect_bot/execution/policy.py:1132-1314)
\*                       DENOTES: remaining orders to dispatch in two-phase order
\*   exec_log          - dispatched orders sequence           (src/fundconnect_bot/execution/policy.py:1132-1314)
\*                       DENOTES: witness trace for SellBeforeBuyDeterministicOrder
\*
\* ACTIONS
\*   EvalHaltedOrNoQuote      - invalid/halted quote skip     (src/fundconnect_bot/execution/policy.py:626-646)
\*   EvalSellFlashCrashGuard  - gap < -12% protective floor   (src/fundconnect_bot/execution/policy.py:655-675)
\*   EvalSellSlicedTwap       - gap in [-12%, -5%) 3-slice    (src/fundconnect_bot/execution/policy.py:676-696)
\*   EvalSellNormal           - gap >= -5% normal sell/trim   (src/fundconnect_bot/execution/policy.py:697-714)
\*   EvalHoldAtTarget         - ideal_qty == cur_qty hold     (src/fundconnect_bot/execution/policy.py:717-735)
\*   EvalBuySkipAdverseGapDown- gap < -3% falling-knife veto  (src/fundconnect_bot/execution/policy.py:737-758)
\*   EvalBuySkipGapUp         - gap > +6% anti-chase veto     (src/fundconnect_bot/execution/policy.py:759-780)
\*   EvalBuyStaged50          - gap in (+3%, +6%] 50% staged  (src/fundconnect_bot/execution/policy.py:781-806)
\*   EvalBuyNormal            - mild gap [-3%, +3%] + wide sp (src/fundconnect_bot/execution/policy.py:807-823)
\*   EvalDefensiveFallback    - 3053.HK absorbs redirected    (src/fundconnect_bot/execution/policy.py:826-887)
\*   SortAndFinalizeBatch     - deterministic Phase A/B sort  (src/fundconnect_bot/execution/policy.py:890-906)
\*   ExecuteBatchStep         - dispatch Phase A before B     (src/fundconnect_bot/execution/policy.py:1132-1314)
\*   BatchDone                - terminal completion stutter   (src/fundconnect_bot/execution/policy.py:1331-1340)
\*
\* FAULTS MODELED: none (faithful transcription; halted/wide-spread quotes in Init domain).
\* =========================================================================

CONSTANTS
    LotS1,
    LotS2,
    InitQtySet,
    BaseBudgetSet,
    DefBaseBudget,
    QuoteRegimes,
    MinNotionalCents

S1     == "0002.HK"   \* Part A HK equity (50..100 -> 0.05, 100..200 -> 0.10, 200..500 -> 0.20)
S2     == "2828.HK"   \* Part D HK equity ETF (20..100 -> 0.02, 100..200 -> 0.10, 200..500 -> 0.20)
SymDef == "3053.HK"   \* Part B HK money-market ETF (all bands -> 0.05)

NonDefSyms == {S1, S2}
AllSyms    == {S1, S2, SymDef}

VARIABLES
    stage,
    cur_qty,
    base_notional,
    q_regime,
    eff_notional,
    sym_redirected,
    redirected_def,
    decisions,
    phase_a_sells,
    phase_b_buys,
    exec_queue,
    exec_log

vars == <<
    stage, cur_qty, base_notional, q_regime, eff_notional,
    sym_redirected, redirected_def, decisions, phase_a_sells,
    phase_b_buys, exec_queue, exec_log
>>

\* ---- Symbol metadata & SEHK contract mapping (src/fundconnect_bot/execution/hkex_rules.py:233-250) ----
LotOf(s) ==
    IF s = S1 THEN LotS1
    ELSE IF s = S2 THEN LotS2
    ELSE 1

IbkrSymbolOf(s) ==
    \* Strips leading zeros on SEHK (src/fundconnect_bot/execution/hkex_rules.py:245-250)
    IF s = S1 THEN "2"
    ELSE IF s = S2 THEN "2828"
    ELSE "3053"

RefCloseCents(s) ==
    \* S1 ref_close = 200.00 HKD (20000c, tests 100..200 / 200..500 HKEX band boundary!)
    \* S2 ref_close = 80.00 HKD  (8000c,  tests Part D 0.02 tick vs Part A 0.05 tick in 50..100!)
    \* DEF ref_close = 120.00 HKD (12000c, Part B 0.05 tick)
    IF s = S1 THEN 20000
    ELSE IF s = S2 THEN 8000
    ELSE 12000

\* ---- Quote regime table (prices in integer cents: 100 = 1.00 HKD) ----
QuoteValid(reg) == reg /= "HALTED"

MidCents(s, reg) ==
    LET rc == RefCloseCents(s) IN
    CASE reg = "HALTED"       -> 0
      [] reg = "FLASH_CRASH"  -> (rc * 85) \div 100
      [] reg = "SEVERE_DOWN"  -> (rc * 92) \div 100
      [] reg = "ADVERSE_DOWN" -> (rc * 96) \div 100
      [] reg = "MILD"         -> IF s = S1 THEN 20005 ELSE (rc * 101) \div 100
      [] reg = "WIDE_MILD"    -> (rc * 101) \div 100
      [] reg = "MOD_UP"       -> (rc * 1045) \div 1000
      [] reg = "SURGE_UP"     -> (rc * 108) \div 100
      [] OTHER                -> rc

SpreadBps(reg) ==
    IF reg = "WIDE_MILD" THEN 90 ELSE 14

AskCents(s, reg) ==
    LET m == MidCents(s, reg) IN
    IF reg = "HALTED" THEN 0
    ELSE IF reg = "WIDE_MILD" THEN m + ((m * 45) \div 10000) + 1
    ELSE m + 4

BidCents(s, reg) ==
    LET m == MidCents(s, reg) IN
    IF reg = "HALTED" THEN 0
    ELSE IF reg = "WIDE_MILD" THEN m - ((m * 45) \div 10000) - 1
    ELSE m - 4

\* Gap in basis points: (mid - ref_close) * 10000 \div ref_close (src/fundconnect_bot/execution/policy.py:650)
GapBps(s, reg) ==
    LET rc == RefCloseCents(s)
        m  == MidCents(s, reg)
    IN ((m - rc) * 10000) \div rc

\* ---- HKEX Spread Table tick & directional rounding (src/fundconnect_bot/execution/hkex_rules.py:87-148) ----
BandAnchorCents(s, px) ==
    IF s = SymDef THEN 0
    ELSE IF px <= 2000 THEN
        IF px <= 1000 /\ s = S1 THEN 50 ELSE IF s = S1 THEN 1000 ELSE 50
    ELSE IF px <= 5000 THEN 2000
    ELSE IF px <= 10000 THEN (IF s = S2 THEN 2000 ELSE 5000)
    ELSE IF px <= 20000 THEN 10000
    ELSE IF px <= 50000 THEN 20000
    ELSE 50000

TickStepCents(s, px) ==
    IF s = SymDef THEN 5
    ELSE IF px <= 2000 THEN 1
    ELSE IF px <= 5000 THEN 2
    ELSE IF px <= 10000 THEN (IF s = S2 THEN 2 ELSE 5)
    ELSE IF px <= 20000 THEN 10
    ELSE IF px <= 50000 THEN 20
    ELSE 50

CeilDiv(a, b) == (a + b - 1) \div b

RoundHkexCents(px, side, s) ==
    \* Exact transcription of round_hkex_price (src/fundconnect_bot/execution/hkex_rules.py:109-148)
    LET anchor == BandAnchorCents(s, px)
        step   == TickStepCents(s, px)
        offset == px - anchor
        ticks  == IF side = "BUY" THEN offset \div step ELSE CeilDiv(offset, step)
        res    == anchor + ticks * step
    IN IF res > 999500 THEN 999500 ELSE res

IsValidHkexTick(s, px) ==
    /\ px >= 1
    /\ px <= 999500
    /\ LET anchor == BandAnchorCents(s, px)
           step   == TickStepCents(s, px)
       IN (px - anchor) % step = 0

\* ---- Board-lot flooring & TWAP slice splitting (src/fundconnect_bot/execution/hkex_rules.py:198-230) ----
FloorToLot(raw_qty, lot) ==
    IF raw_qty <= 0 THEN 0
    ELSE (raw_qty \div lot) * lot

SplitIntoLotSlices(total_qty, lot, num_slices) ==
    IF total_qty <= 0 THEN << >>
    ELSE IF num_slices <= 1 \/ total_qty <= lot THEN << total_qty >>
    ELSE LET total_lots == total_qty \div lot
             odd_rem    == total_qty % lot
         IN IF total_lots = 0 THEN << odd_rem >>
            ELSE LET eff == IF num_slices < total_lots THEN num_slices ELSE total_lots
                     base_l == total_lots \div eff
                     extra  == total_lots % eff
                     RawSlice(i) ==
                        (base_l + (IF i <= extra THEN 1 ELSE 0)) * lot
                        + (IF i = eff THEN odd_rem ELSE 0)
                 IN [i \in 1..eff |-> RawSlice(i)]

\* ---- Style & raw limit selection (src/fundconnect_bot/execution/policy.py:194-230) ----
Min3(a, b, c) ==
    LET m == IF a < b THEN a ELSE b IN IF m < c THEN m ELSE c

Max3(a, b, c) ==
    LET m == IF a > b THEN a ELSE b IN IF m > c THEN m ELSE c

SelectStyleAndLimit(s, side, reg, patient_override, flash_floor) ==
    LET m    == MidCents(s, reg)
        rc   == RefCloseCents(s)
        wide == SpreadBps(reg) > 60
    IN IF flash_floor THEN
        \* raw_floor = ref_close * (1 - 0.12) (src/fundconnect_bot/execution/policy.py:209-213)
        LET raw_f == (rc * 88) \div 100
        IN [style |-> "PROTECTIVE_FLOOR_LIMIT", raw_px |-> raw_f, lmt_px |-> RoundHkexCents(raw_f, "SELL", s)]
    ELSE IF wide \/ patient_override THEN
        \* PASSIVE_MID_LIMIT on HK (src/fundconnect_bot/execution/policy.py:214-218)
        [style |-> "PASSIVE_MID_LIMIT", raw_px |-> m, lmt_px |-> RoundHkexCents(m, side, s)]
    ELSE IF side = "BUY" THEN
        \* min(ask, mid*(1+8bps), mid*(1+25bps)) (src/fundconnect_bot/execution/policy.py:221-225)
        LET init_cap == (m * 10008) \div 10000
            max_cap  == (m * 10025) \div 10000
            raw_b    == Min3(AskCents(s, reg), init_cap, max_cap)
        IN [style |-> "MARKETABLE_LIMIT", raw_px |-> raw_b, lmt_px |-> RoundHkexCents(raw_b, "BUY", s)]
    ELSE
        \* max(bid, mid*(1-8bps), mid*(1-25bps)) (src/fundconnect_bot/execution/policy.py:227-230)
        LET init_flr == CeilDiv(m * 9992, 10000)
            min_cap  == CeilDiv(m * 9975, 10000)
            raw_s    == Max3(BidCents(s, reg), init_flr, min_cap)
        IN [style |-> "MARKETABLE_LIMIT", raw_px |-> raw_s, lmt_px |-> RoundHkexCents(raw_s, "SELL", s)]

\* ---- Emit helper matching _emit_orders_and_decision (src/fundconnect_bot/execution/policy.py:356-613) ----
MakeOrderSeq(s, side, slices, style, raw_px, lmt_px, act_code) ==
    [i \in 1..Len(slices) |-> [
        symbol          |-> s,
        ibkr_symbol     |-> IbkrSymbolOf(s),
        side            |-> side,
        phase           |-> IF side = "SELL" THEN "PHASE_A_SELL" ELSE "PHASE_B_BUY",
        quantity        |-> slices[i],
        lot_size        |-> LotOf(s),
        raw_limit_cents |-> raw_px,
        limit_cents     |-> lmt_px,
        style           |-> style,
        slice_index     |-> i,
        slice_count     |-> Len(slices),
        reason          |-> act_code
    ]]

NextStage(s) == IF s = S1 THEN S2 ELSE SymDef

CurrentSym ==
    IF stage = "S1" THEN S1
    ELSE IF stage = "S2" THEN S2
    ELSE SymDef

EmitDecisionAndOrders(s, act, cur_q, desired_q, base_n, eff_n, redir, reg) ==
    LET m         == MidCents(s, reg)
        lot       == LotOf(s)
        raw_delta == desired_q - cur_q
        no_trade  == \/ act \in {"SKIP_GAP_UP", "SKIP_ADVERSE_GAP_DOWN", "HOLD_AT_TARGET"}
                     \/ (raw_delta = 0 /\ act /= "HOLD_FLASH_CRASH_GUARD")
    IN IF ~QuoteValid(reg) THEN
        /\ eff_notional'   = [eff_notional EXCEPT ![s] = eff_n]
        /\ sym_redirected' = [sym_redirected EXCEPT ![s] = 0]
        /\ decisions'      = [decisions EXCEPT ![s] = [action |-> "HALTED_OR_NO_QUOTE", desired_qty |-> cur_q, delta_qty |-> 0]]
        /\ UNCHANGED <<phase_a_sells, phase_b_buys>>
    ELSE IF no_trade THEN
        LET final_act == IF raw_delta = 0 /\ act = "BUY_NORMAL" THEN "HOLD_AT_TARGET" ELSE act
        IN /\ eff_notional'   = [eff_notional EXCEPT ![s] = eff_n]
           /\ sym_redirected' = [sym_redirected EXCEPT ![s] = redir]
           /\ decisions'      = [decisions EXCEPT ![s] = [action |-> final_act, desired_qty |-> desired_q, delta_qty |-> 0]]
           /\ UNCHANGED <<phase_a_sells, phase_b_buys>>
    ELSE
        LET side      == IF raw_delta < 0 \/ act = "HOLD_FLASH_CRASH_GUARD" THEN "SELL" ELSE "BUY"
            gross_qty == IF side = "SELL" THEN cur_q - desired_q ELSE desired_q - cur_q
            net_qty   == IF side = "SELL" THEN gross_qty ELSE FloorToLot(gross_qty, lot)
            full_exit == (side = "SELL" /\ desired_q = 0)
        IN IF net_qty = 0 THEN
            /\ eff_notional'   = [eff_notional EXCEPT ![s] = eff_n]
            /\ sym_redirected' = [sym_redirected EXCEPT ![s] = redir]
            /\ decisions'      = [decisions EXCEPT ![s] = [action |-> "SKIP_ALREADY_WORKING_OR_DONE", desired_qty |-> desired_q, delta_qty |-> 0]]
            /\ UNCHANGED <<phase_a_sells, phase_b_buys>>
        ELSE IF ~full_exit /\ (net_qty * m) < MinNotionalCents THEN
            \* Min-notional churn filter (src/fundconnect_bot/execution/policy.py:478-504)
            /\ eff_notional'   = [eff_notional EXCEPT ![s] = eff_n]
            /\ sym_redirected' = [sym_redirected EXCEPT ![s] = redir]
            /\ decisions'      = [decisions EXCEPT ![s] = [action |-> "SKIP_BELOW_MIN_NOTIONAL", desired_qty |-> cur_q, delta_qty |-> 0]]
            /\ UNCHANGED <<phase_a_sells, phase_b_buys>>
        ELSE
            LET patient == act \in {"BUY_STAGED_50", "SELL_SLICED_TWAP"}
                flash   == (act = "HOLD_FLASH_CRASH_GUARD")
                sl      == SelectStyleAndLimit(s, side, reg, patient, flash)
                nslices == IF act = "SELL_SLICED_TWAP" THEN 3 ELSE 1
                slices  == SplitIntoLotSlices(net_qty, lot, nslices)
                ord_seq == MakeOrderSeq(s, side, slices, sl.style, sl.raw_px, sl.lmt_px, act)
                s_delta == IF side = "SELL" THEN -net_qty ELSE net_qty
            IN /\ eff_notional'   = [eff_notional EXCEPT ![s] = eff_n]
               /\ sym_redirected' = [sym_redirected EXCEPT ![s] = redir]
               /\ decisions'      = [decisions EXCEPT ![s] = [action |-> act, desired_qty |-> desired_q, delta_qty |-> s_delta]]
               /\ IF side = "SELL"
                  THEN /\ phase_a_sells' = phase_a_sells \o ord_seq
                       /\ UNCHANGED phase_b_buys
                  ELSE /\ phase_b_buys'  = phase_b_buys \o ord_seq
                       /\ UNCHANGED phase_a_sells

\* ---- Deterministic order comparator (src/fundconnect_bot/execution/policy.py:890-891) ----
SymRank(s) == IF s = S1 THEN 1 ELSE IF s = S2 THEN 2 ELSE 3

SellBefore(o1, o2) ==
    \/ SymRank(o1.symbol) < SymRank(o2.symbol)
    \/ (o1.symbol = o2.symbol /\ o1.slice_index <= o2.slice_index)

BuyKeyRank(s) ==
    \* (o.symbol == defensive_sym, o.symbol): non-defensive first (1, 2), defensive last (3)
    IF s = S1 THEN 1 ELSE IF s = S2 THEN 2 ELSE 3

BuyBefore(o1, o2) ==
    \/ BuyKeyRank(o1.symbol) < BuyKeyRank(o2.symbol)
    \/ (o1.symbol = o2.symbol /\ o1.slice_index <= o2.slice_index)

RECURSIVE InsertSell(_, _)
InsertSell(x, seq) ==
    IF Len(seq) = 0 THEN << x >>
    ELSE IF SellBefore(x, Head(seq)) THEN << x >> \o seq
    ELSE << Head(seq) >> \o InsertSell(x, Tail(seq))

RECURSIVE SortSells(_)
SortSells(seq) ==
    IF Len(seq) <= 1 THEN seq
    ELSE InsertSell(Head(seq), SortSells(Tail(seq)))

RECURSIVE InsertBuy(_, _)
InsertBuy(x, seq) ==
    IF Len(seq) = 0 THEN << x >>
    ELSE IF BuyBefore(x, Head(seq)) THEN << x >> \o seq
    ELSE << Head(seq) >> \o InsertBuy(x, Tail(seq))

RECURSIVE SortBuys(_)
SortBuys(seq) ==
    IF Len(seq) <= 1 THEN seq
    ELSE InsertBuy(Head(seq), SortBuys(Tail(seq)))

\* ---- Initial state ----
Init ==
    /\ stage = "S1"
    /\ cur_qty \in [AllSyms -> InitQtySet]
    /\ base_notional \in {
           [s \in AllSyms |-> IF s = S1 THEN b1 ELSE IF s = S2 THEN b2 ELSE DefBaseBudget] :
           b1 \in BaseBudgetSet, b2 \in BaseBudgetSet
       }
    /\ q_regime \in [NonDefSyms -> QuoteRegimes]
    /\ eff_notional   = [s \in AllSyms |-> 0]
    /\ sym_redirected = [s \in AllSyms |-> 0]
    /\ redirected_def = 0
    /\ decisions      = [s \in AllSyms |-> [action |-> "NONE", desired_qty |-> 0, delta_qty |-> 0]]
    /\ phase_a_sells  = << >>
    /\ phase_b_buys   = << >>
    /\ exec_queue     = << >>
    /\ exec_log       = << >>

\* ---- Step 1: Non-defensive symbol evaluation actions (src/fundconnect_bot/execution/policy.py:616-824) ----
AdvanceNonDef ==
    stage' = IF stage = "S1" THEN "S2" ELSE "DEF"

EvalHaltedOrNoQuote ==
    \* src/fundconnect_bot/execution/policy.py:626-646
    /\ stage \in {"S1", "S2"}
    /\ LET s   == CurrentSym
           reg == q_regime[s]
           cq  == cur_qty[s]
           bn  == base_notional[s]
       IN /\ ~QuoteValid(reg)
          /\ EmitDecisionAndOrders(s, "HALTED_OR_NO_QUOTE", cq, cq, bn, bn, 0, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalSellFlashCrashGuard ==
    \* src/fundconnect_bot/execution/policy.py:655-675 (gap_pct < -12.0%)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           gap   == GapBps(s, reg)
       IN /\ QuoteValid(reg)
          /\ ideal < cq
          /\ gap < -1200
          /\ EmitDecisionAndOrders(s, "HOLD_FLASH_CRASH_GUARD", cq, ideal, bn, bn, 0, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalSellSlicedTwap ==
    \* src/fundconnect_bot/execution/policy.py:676-696 (-12.0% <= gap_pct < -5.0%)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           gap   == GapBps(s, reg)
       IN /\ QuoteValid(reg)
          /\ ideal < cq
          /\ gap >= -1200
          /\ gap < -500
          /\ EmitDecisionAndOrders(s, "SELL_SLICED_TWAP", cq, ideal, bn, bn, 0, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalSellNormal ==
    \* src/fundconnect_bot/execution/policy.py:697-714 (gap_pct >= -5.0%)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           gap   == GapBps(s, reg)
       IN /\ QuoteValid(reg)
          /\ ideal < cq
          /\ gap >= -500
          /\ EmitDecisionAndOrders(s, "SELL_NORMAL", cq, ideal, bn, bn, 0, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalHoldAtTarget ==
    \* src/fundconnect_bot/execution/policy.py:717-735 (ideal_qty_mid == cur_qty or inc_budget <= 0)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           inc   == IF bn > cq * m THEN bn - cq * m ELSE 0
       IN /\ QuoteValid(reg)
          /\ ideal >= cq
          /\ (ideal = cq \/ inc <= 0)
          /\ EmitDecisionAndOrders(s, "HOLD_AT_TARGET", cq, cq, bn, bn, 0, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalBuySkipAdverseGapDown ==
    \* src/fundconnect_bot/execution/policy.py:737-758 (gap_pct < -3.0% falling-knife veto)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           inc   == bn - cq * m
           gap   == GapBps(s, reg)
       IN /\ QuoteValid(reg)
          /\ ideal > cq
          /\ inc > 0
          /\ gap < -300
          /\ redirected_def' = redirected_def + inc
          /\ EmitDecisionAndOrders(s, "SKIP_ADVERSE_GAP_DOWN", cq, cq, bn, cq * m, inc, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalBuySkipGapUp ==
    \* src/fundconnect_bot/execution/policy.py:759-780 (gap_pct > +6.0% anti-chase circuit breaker)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           inc   == bn - cq * m
           gap   == GapBps(s, reg)
       IN /\ QuoteValid(reg)
          /\ ideal > cq
          /\ inc > 0
          /\ gap > 600
          /\ redirected_def' = redirected_def + inc
          /\ EmitDecisionAndOrders(s, "SKIP_GAP_UP", cq, cq, bn, cq * m, inc, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalBuyStaged50 ==
    \* src/fundconnect_bot/execution/policy.py:781-806 (+3.0% < gap_pct <= +6.0% -> 50% staged + 50% redirect)
    /\ stage \in {"S1", "S2"}
    /\ LET s        == CurrentSym
           reg      == q_regime[s]
           cq       == cur_qty[s]
           bn       == base_notional[s]
           m        == MidCents(s, reg)
           ideal    == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           inc      == bn - cq * m
           gap      == GapBps(s, reg)
           keep_inc == inc \div 2
           redir    == inc - keep_inc
           staged_n == cq * m + keep_inc
           staged_q == FloorToLot(staged_n \div m, LotOf(s))
       IN /\ QuoteValid(reg)
          /\ ideal > cq
          /\ inc > 0
          /\ gap > 300
          /\ gap <= 600
          /\ redirected_def' = redirected_def + redir
          /\ EmitDecisionAndOrders(s, "BUY_STAGED_50", cq, staged_q, bn, staged_n, redir, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, exec_queue, exec_log>>
          /\ AdvanceNonDef

EvalBuyNormal ==
    \* src/fundconnect_bot/execution/policy.py:807-823 (-3.0% <= gap_pct <= +3.0%)
    /\ stage \in {"S1", "S2"}
    /\ LET s     == CurrentSym
           reg   == q_regime[s]
           cq    == cur_qty[s]
           bn    == base_notional[s]
           m     == MidCents(s, reg)
           ideal == IF bn > 0 THEN FloorToLot(bn \div m, LotOf(s)) ELSE 0
           inc   == bn - cq * m
           gap   == GapBps(s, reg)
       IN /\ QuoteValid(reg)
          /\ ideal > cq
          /\ inc > 0
          /\ gap >= -300
          /\ gap <= 300
          /\ EmitDecisionAndOrders(s, "BUY_NORMAL", cq, ideal, bn, bn, 0, reg)
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>
          /\ AdvanceNonDef

\* ---- Step 2: Defensive fallback sleeve evaluation (src/fundconnect_bot/execution/policy.py:826-887) ----
EvalDefensiveFallback ==
    /\ stage = "DEF"
    /\ LET cq      == cur_qty[SymDef]
           def_b   == base_notional[SymDef]
           def_eff == def_b + redirected_def
           m       == MidCents(SymDef, "MILD")
           des_q   == FloorToLot(def_eff \div m, LotOf(SymDef))
           act     == IF des_q < cq THEN "SELL_NORMAL"
                      ELSE IF redirected_def > 0 THEN "BUY_DEFENSIVE_FALLBACK"
                      ELSE "BUY_NORMAL"
       IN /\ IF def_eff > 0 \/ cq > 0
             THEN EmitDecisionAndOrders(SymDef, act, cq, des_q, def_b, def_eff, 0, "MILD")
             ELSE /\ eff_notional'   = [eff_notional EXCEPT ![SymDef] = 0]
                  /\ sym_redirected' = [sym_redirected EXCEPT ![SymDef] = 0]
                  /\ decisions'      = [decisions EXCEPT ![SymDef] = [action |-> "HOLD_AT_TARGET", desired_qty |-> 0, delta_qty |-> 0]]
                  /\ UNCHANGED <<phase_a_sells, phase_b_buys>>
          /\ stage' = "SORT"
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, redirected_def, exec_queue, exec_log>>

\* ---- Step 3: Deterministic sort & two-phase queue (src/fundconnect_bot/execution/policy.py:890-906) ----
SortAndFinalizeBatch ==
    /\ stage = "SORT"
    /\ LET sorted_a == SortSells(phase_a_sells)
           sorted_b == SortBuys(phase_b_buys)
           combined == sorted_a \o sorted_b
       IN /\ phase_a_sells' = sorted_a
          /\ phase_b_buys'  = sorted_b
          /\ exec_queue'    = combined
          /\ stage'         = IF Len(combined) > 0 THEN "EXEC" ELSE "DONE"
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, eff_notional, sym_redirected, redirected_def, decisions, exec_log>>

\* ---- Step 4: Two-phase execution dispatch (src/fundconnect_bot/execution/policy.py:1132-1314) ----
ExecuteBatchStep ==
    /\ stage = "EXEC"
    /\ Len(exec_queue) > 0
    /\ LET nxt  == Head(exec_queue)
           rest == Tail(exec_queue)
       IN /\ exec_log'   = Append(exec_log, nxt)
          /\ exec_queue' = rest
          /\ stage'      = IF Len(rest) = 0 THEN "DONE" ELSE "EXEC"
          /\ UNCHANGED <<cur_qty, base_notional, q_regime, eff_notional, sym_redirected, redirected_def, decisions, phase_a_sells, phase_b_buys>>

BatchDone ==
    \* src/fundconnect_bot/execution/policy.py:1331-1340
    /\ stage = "DONE"
    /\ UNCHANGED vars

Next ==
    \/ EvalHaltedOrNoQuote
    \/ EvalSellFlashCrashGuard
    \/ EvalSellSlicedTwap
    \/ EvalSellNormal
    \/ EvalHoldAtTarget
    \/ EvalBuySkipAdverseGapDown
    \/ EvalBuySkipGapUp
    \/ EvalBuyStaged50
    \/ EvalBuyNormal
    \/ EvalDefensiveFallback
    \/ SortAndFinalizeBatch
    \/ ExecuteBatchStep
    \/ BatchDone

Spec == Init /\ [][Next]_vars

\* =========================================================================
\* Developer-Expectation Invariants
\* =========================================================================

TypeOK ==
    /\ stage \in {"S1", "S2", "DEF", "SORT", "EXEC", "DONE"}
    /\ cur_qty \in [AllSyms -> Nat]
    /\ base_notional \in [AllSyms -> Nat]
    /\ eff_notional \in [AllSyms -> Nat]
    /\ sym_redirected \in [AllSyms -> Nat]
    /\ redirected_def \in Nat

Max0(x) == IF x > 0 THEN x ELSE 0

SymMark(s) ==
    IF s = SymDef THEN cur_qty[SymDef] * MidCents(SymDef, "MILD")
    ELSE IF QuoteValid(q_regime[s]) THEN cur_qty[s] * MidCents(s, q_regime[s])
    ELSE 0

BudgetConservation ==
    /\ redirected_def >= 0
    /\ (stage = "DEF" => redirected_def = sym_redirected[S1] + sym_redirected[S2])
    /\ \A s \in NonDefSyms :
        (decisions[s].action /= "NONE" /\ QuoteValid(q_regime[s]) /\ SymMark(s) <= base_notional[s])
            => (eff_notional[s] + sym_redirected[s] <= base_notional[s])
    /\ (stage \in {"SORT", "EXEC", "DONE"} =>
        LET inc_eff == Max0(eff_notional[S1] - SymMark(S1))
                     + Max0(eff_notional[S2] - SymMark(S2))
                     + Max0(eff_notional[SymDef] - SymMark(SymDef))
            inc_pln == Max0(base_notional[S1] - SymMark(S1))
                     + Max0(base_notional[S2] - SymMark(S2))
                     + Max0(base_notional[SymDef] - SymMark(SymDef))
            planned_total == base_notional[S1] + base_notional[S2] + base_notional[SymDef]
        IN /\ eff_notional[SymDef] = base_notional[SymDef] + redirected_def
           /\ inc_eff <= inc_pln
           /\ ((cur_qty[S1] = 0 /\ cur_qty[S2] = 0 /\ cur_qty[SymDef] = 0)
                => eff_notional[S1] + eff_notional[S2] + eff_notional[SymDef] <= planned_total))

NoNegativeOrNaNQty ==
    /\ \A s \in AllSyms :
        /\ decisions[s].desired_qty >= 0
        /\ (s \in NonDefSyms /\ ~QuoteValid(q_regime[s]) /\ decisions[s].action /= "NONE")
            => (decisions[s].action = "HALTED_OR_NO_QUOTE" /\ decisions[s].delta_qty = 0)
    /\ \A i \in 1..Len(phase_a_sells) :
        /\ phase_a_sells[i].quantity > 0
        /\ phase_a_sells[i].limit_cents > 0
        /\ ~(phase_a_sells[i].symbol \in NonDefSyms /\ ~QuoteValid(q_regime[phase_a_sells[i].symbol]))
    /\ \A i \in 1..Len(phase_b_buys) :
        /\ phase_b_buys[i].quantity > 0
        /\ phase_b_buys[i].limit_cents > 0
        /\ ~(phase_b_buys[i].symbol \in NonDefSyms /\ ~QuoteValid(q_regime[phase_b_buys[i].symbol]))

OrderMicrostructureOK(o) ==
    /\ IsValidHkexTick(o.symbol, o.limit_cents)
    /\ (o.side = "BUY"  => o.limit_cents <= o.raw_limit_cents)
    /\ (o.side = "SELL" => o.limit_cents >= o.raw_limit_cents)
    /\ o.ibkr_symbol \in {"2", "2828", "3053"}
    /\ (o.side = "BUY"  => o.quantity % o.lot_size = 0)
    /\ ((o.side = "SELL" /\ cur_qty[o.symbol] % o.lot_size = 0) => o.quantity % o.lot_size = 0)

HkexTickAndBoardLotValid ==
    /\ \A i \in 1..Len(phase_a_sells) : OrderMicrostructureOK(phase_a_sells[i])
    /\ \A i \in 1..Len(phase_b_buys)  : OrderMicrostructureOK(phase_b_buys[i])

SellBeforeBuyDeterministicOrder ==
    /\ \A i \in 1..Len(phase_a_sells) :
        phase_a_sells[i].side = "SELL" /\ phase_a_sells[i].phase = "PHASE_A_SELL"
    /\ \A i \in 1..Len(phase_b_buys) :
        phase_b_buys[i].side = "BUY" /\ phase_b_buys[i].phase = "PHASE_B_BUY"
    /\ (stage \in {"EXEC", "DONE"} =>
        /\ \A i \in 1..Len(phase_a_sells) :
            \A j \in (i+1)..Len(phase_a_sells) : SellBefore(phase_a_sells[i], phase_a_sells[j])
        /\ \A i \in 1..Len(phase_b_buys) :
            \A j \in (i+1)..Len(phase_b_buys) : BuyBefore(phase_b_buys[i], phase_b_buys[j]))
    /\ \A i \in 1..Len(exec_log) :
        \A j \in (i+1)..Len(exec_log) :
            ~(exec_log[i].side = "BUY" /\ exec_log[j].side = "SELL")
=============================================================================
