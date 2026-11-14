-------------------- MODULE L2_TwoPhaseCashSettlement --------------------
EXTENDS Integers, FiniteSets, TLC

\* ============================ var <-> code map ============================
\* CONSTANTS (read from src/fundconnect_bot/config.py:209-239 & cli.py:287-305)
\*   Scenarios          - initial cash/settlement test scenarios (src/fundconnect_bot/cli.py:272-305)
\*   RequireSettledHkd  - config.require_settled_hkd (TRUE)      (src/fundconnect_bot/config.py:235)
\*   LotUnitCost        - per-lot worst-case buy outlay + fee    (src/fundconnect_bot/execution/policy.py:909-927)
\*
\* VARIABLES
\*   scenario           - active scenario identifier             (src/fundconnect_bot/execution/policy.py:1050)
\*                        DENOTES: immutable initial portfolio/settlement setup
\*   market             - market parameter ("US" or "HK")        (src/fundconnect_bot/execution/policy.py:1054)
\*                        DENOTES: target sleeve currency/settlement regime
\*   step               - control cursor in execute_market_rebalance (src/fundconnect_bot/execution/policy.py:1081-1231)
\*                        DENOTES: {"PHASE_A","POST_SELL_CASH","FX_STEP","COMPUTE_CAP","PHASE_B","SETTLEMENT"}
\*   day                - clearing calendar day in 0..3          (src/fundconnect_bot/execution/broker.py:495-500)
\*                        DENOTES: T+0 execution session followed by T+1, T+2, T+3 settlement days
\*   settled_cash       - IBKR SettledCash tag per currency      (src/fundconnect_bot/execution/broker.py:312-313, 323)
\*                        DENOTES: cleared cash balance (negative => overnight margin loan accruing interest)
\*   pending_settle     - unsettled clearing pipeline [ccy][d]   (src/fundconnect_bot/execution/broker.py:308-309)
\*                        DENOTES: net cash delta settling on day d \in {1, 2}
\*   margin_bonus_usd   - Margin AvailableFunds collateral in USD(src/fundconnect_bot/execution/broker.py:314-315, 324-328)
\*                        DENOTES: excess margin purchasing power above USD CashBalance
\*   pre_cash_snap      - `cash` snapshot at entry               (src/fundconnect_bot/execution/policy.py:1082, 1177)
\*                        DENOTES: pre-Phase-A CashSnap for active market currency
\*   after_sells_snap   - `after_sells_c` snapshot               (src/fundconnect_bot/execution/policy.py:1141)
\*                        DENOTES: post-Phase-A CashSnap before FX conversion
\*   phase_a_req        - planned Phase A sell proceeds          (src/fundconnect_bot/execution/policy.py:1118)
\*                        DENOTES: fee-adjusted net proceeds if Phase A sells fill
\*   realized_sell_net  - _realized_sell_net_proceeds            (src/fundconnect_bot/execution/policy.py:930-951, 1178)
\*                        DENOTES: realized same-session Phase A sell net proceeds
\*   phase_b_req        - uncapped total_required for Phase B    (src/fundconnect_bot/execution/policy.py:986)
\*                        DENOTES: sum of OrderSpec quantity * _unit_cost_with_fee
\*   bootstrap_fx_req   - bootstrap_fx_usd CLI parameter         (src/fundconnect_bot/execution/policy.py:1057, 1145)
\*                        DENOTES: USD amount requested for HK sleeve FX conversion
\*   allow_fx_convert   - allow_fx_convert CLI parameter         (src/fundconnect_bot/execution/policy.py:1058, 1146)
\*                        DENOTES: whether live USD.HKD order is submitted
\*   fx_submitted       - whether convert_usd_to_hkd placed order(src/fundconnect_bot/execution/broker.py:526)
\*                        DENOTES: TRUE iff live Forex("USDHKD") order transmitted
\*   fx_exchange        - fx_contract.exchange used on FX order  (src/fundconnect_bot/execution/broker.py:514)
\*                        DENOTES: "NONE" | "IDEALPRO" | "FXCONV"
\*   virtual_fx_open    - TWS FX Portfolio virtual position flag (src/fundconnect_bot/execution/broker.py:514-526)
\*                        DENOTES: TRUE when USD.HKD trade routed to IDEALPRO instead of FXCONV
\*   reserve_usd        - unspent USD reserved when ~allow_fx    (src/fundconnect_bot/execution/policy.py:1155)
\*                        DENOTES: reserve_amount passed to compute_spendable_cash
\*   spendable_cash     - spendable returned before Phase B cap  (src/fundconnect_bot/execution/policy.py:1200)
\*                        DENOTES: non-borrowed cash cap passed to apply_cash_cap_to_buys
\*   capped_buy_outlay  - total outlay of capped_buys            (src/fundconnect_bot/execution/policy.py:1202-1204)
\*                        DENOTES: actual Phase B buy outlay after apply_cash_cap_to_buys
\* =========================================================================

CONSTANTS
    Scenarios,
    RequireSettledHkd,
    LotUnitCost

Ccys == {"USD", "HKD"}
SettleDays == {1, 2}
FxRate == 8   \* Scaled integer USDHKD rate

VARIABLES
    scenario,
    market,
    step,
    day,
    settled_cash,
    pending_settle,
    margin_bonus_usd,
    pre_cash_snap,
    after_sells_snap,
    phase_a_req,
    realized_sell_net,
    phase_b_req,
    bootstrap_fx_req,
    allow_fx_convert,
    fx_submitted,
    fx_exchange,
    virtual_fx_open,
    reserve_usd,
    spendable_cash,
    capped_buy_outlay

vars == <<
    scenario, market, step, day, settled_cash, pending_settle, margin_bonus_usd,
    pre_cash_snap, after_sells_snap, phase_a_req, realized_sell_net, phase_b_req,
    bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open,
    reserve_usd, spendable_cash, capped_buy_outlay
>>

Min2(a, b) == IF a < b THEN a ELSE b
Max2(a, b) == IF a > b THEN a ELSE b
Min3(a, b, c) == Min2(Min2(a, b), c)

MarketCcy(m) == IF m = "HK" THEN "HKD" ELSE "USD"
EquitySettleDay(m) == IF m = "US" THEN 1 ELSE 2   \* US equities T+1; SEHK equities T+2

\* Unsettled cash still in clearing pipeline after `cur_day`
UnsettledAfter(p_map, c, cur_day) ==
    (IF 1 > cur_day THEN p_map[c][1] ELSE 0)
    + (IF 2 > cur_day THEN p_map[c][2] ELSE 0)

\* Exact transcription of IbAsyncBrokerAdapter.get_cash (src/fundconnect_bot/execution/broker.py:280-334)
\* - CashBalance = settled_cash + unsettled trades/FX
\* - AvailableFunds emitted in Base Currency (USD = CashBalance + margin_bonus_usd); None on HKD -> falls back to total
BrokerCashSnap(s_cash, p_map, m_bonus, cur_day, c) ==
    LET total   == s_cash[c] + UnsettledAfter(p_map, c, cur_day)
        settled == s_cash[c]
        avail   == IF c = "USD" THEN total + m_bonus ELSE total
    IN [total |-> total, settled |-> settled, avail |-> avail]

\* Exact transcription of compute_spendable_cash (src/fundconnect_bot/execution/policy.py:954-966)
ComputeSpendable(c, snap, res_amt) ==
    LET base == IF c = "HKD" /\ RequireSettledHkd
                THEN Min3(snap.total, snap.settled, snap.avail)
                ELSE Min2(snap.total, snap.avail)
    IN Max2(0, base - Max2(0, res_amt))

\* Exact transcription of apply_cash_cap_to_buys (src/fundconnect_bot/execution/policy.py:968-1009)
CapBuysOutlay(req_outlay, spendable) ==
    IF req_outlay <= 0 \/ spendable <= 0 THEN 0
    ELSE IF req_outlay <= spendable THEN req_outlay
    ELSE (spendable \div LotUnitCost) * LotUnitCost

ZeroSnap == [total |-> 0, settled |-> 0, avail |-> 0]

\* ---- Scenario initializers ----
InitForScenario(sc) ==
    CASE sc = "SAME_SESSION_US" ->
            \* $100k SGOV.US sell (T+1) funding $90k US buys (T+1), $30k reserved (allow_fx_convert=FALSE)
            /\ market           = "US"
            /\ settled_cash     = [c \in Ccys |-> 0]
            /\ pending_settle   = [c \in Ccys |-> [d \in SettleDays |-> 0]]
            /\ margin_bonus_usd = 150
            /\ phase_a_req      = 100
            /\ phase_b_req      = 90
            /\ bootstrap_fx_req = 30
            /\ allow_fx_convert = FALSE
      [] sc = "HK_UNSETTLED_FX_HKD" ->
            \* Unsettled T+2 FX HKD (+240k in day 2, settled_cash["HKD"] = 0) before HK Phase B buys
            /\ market           = "HK"
            /\ settled_cash     = [c \in Ccys |-> 0]
            /\ pending_settle   = [c \in Ccys |-> [d \in SettleDays |-> IF c = "HKD" /\ d = 2 THEN 240 ELSE 0]]
            /\ margin_bonus_usd = 0
            /\ phase_a_req      = 0
            /\ phase_b_req      = 200
            /\ bootstrap_fx_req = 0
            /\ allow_fx_convert = FALSE
      [] sc = "UNSETTLED_FX_USD" ->
            \* Unsettled T+2 FX USD (+100k settling day 2, settled_cash["USD"] = 0) before US T+1 buys
            /\ market           = "US"
            /\ settled_cash     = [c \in Ccys |-> 0]
            /\ pending_settle   = [c \in Ccys |-> [d \in SettleDays |-> IF c = "USD" /\ d = 2 THEN 100 ELSE 0]]
            /\ margin_bonus_usd = 100
            /\ phase_a_req      = 0
            /\ phase_b_req      = 80
            /\ bootstrap_fx_req = 0
            /\ allow_fx_convert = FALSE
      [] sc = "MONDAY_BOOTSTRAP_FX" ->
            \* Monday US bootstrap: $100k SGOV.US sell (T+1) + $30k USD.HKD FX conversion (T+2) + $60k US buys (T+1)
            /\ market           = "US"
            /\ settled_cash     = [c \in Ccys |-> 0]
            /\ pending_settle   = [c \in Ccys |-> [d \in SettleDays |-> 0]]
            /\ margin_bonus_usd = 150
            /\ phase_a_req      = 100
            /\ phase_b_req      = 60
            /\ bootstrap_fx_req = 30
            /\ allow_fx_convert = TRUE

Init ==
    /\ scenario \in Scenarios
    /\ InitForScenario(scenario)
    /\ step              = "PHASE_A"
    /\ day               = 0
    /\ pre_cash_snap     = BrokerCashSnap(settled_cash, pending_settle, margin_bonus_usd, 0, MarketCcy(market))
    /\ after_sells_snap  = ZeroSnap
    /\ realized_sell_net = 0
    /\ fx_submitted      = FALSE
    /\ fx_exchange       = "NONE"
    /\ virtual_fx_open   = FALSE
    /\ reserve_usd       = 0
    /\ spendable_cash    = 0
    /\ capped_buy_outlay = 0

\* ---- Step 1: Phase A sells (src/fundconnect_bot/execution/policy.py:1116-1122) ----

ExecPhaseASellFilled ==
    \* Phase A sells fill in-session and schedule proceeds on EquitySettleDay(market)
    \* (src/fundconnect_bot/execution/policy.py:1116-1122, 930-951)
    /\ step = "PHASE_A"
    /\ phase_a_req > 0
    /\ LET ccy == MarketCcy(market)
           sd  == EquitySettleDay(market)
       IN /\ pending_settle'    = [pending_settle EXCEPT ![ccy][sd] = @ + phase_a_req]
          /\ realized_sell_net' = phase_a_req
          /\ step'              = "POST_SELL_CASH"
          /\ UNCHANGED <<scenario, market, day, settled_cash, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, reserve_usd, spendable_cash, capped_buy_outlay>>

ExecPhaseASellUnfilled ==
    \* Either no Phase A sells exist, or Phase A limit order stays WORKING after walk steps
    \* (src/fundconnect_bot/execution/policy.py:1027-1046, 1116-1126)
    /\ step = "PHASE_A"
    /\ realized_sell_net' = 0
    /\ step'              = "POST_SELL_CASH"
    /\ UNCHANGED <<scenario, market, day, settled_cash, pending_settle, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, reserve_usd, spendable_cash, capped_buy_outlay>>

\* ---- Step 2: Refresh cash after Phase A sells (src/fundconnect_bot/execution/policy.py:1140-1141) ----

RefreshCashAfterSells ==
    /\ step = "POST_SELL_CASH"
    /\ LET ccy == MarketCcy(market) IN
       /\ after_sells_snap' = BrokerCashSnap(settled_cash, pending_settle, margin_bonus_usd, day, ccy)
       /\ step'             = "FX_STEP"
       /\ UNCHANGED <<scenario, market, day, settled_cash, pending_settle, margin_bonus_usd, pre_cash_snap, phase_a_req, realized_sell_net, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, reserve_usd, spendable_cash, capped_buy_outlay>>

\* ---- Step 3: Optional bootstrap USD.HKD FX conversion (src/fundconnect_bot/execution/policy.py:1145-1155) ----

ExecBootstrapFx ==
    \* policy.py:1145-1148 calls broker.convert_usd_to_hkd(bootstrap_fx_usd, dry_run=False)
    \* WITHOUT checking settled_cash["USD"] or realized_sell_net!
    \* broker.py:514 constructs ib_mod.Forex("USDHKD") (default exchange="IDEALPRO", opening a Virtual FX Position)
    \* and sells USD / buys HKD for T+2 settlement (src/fundconnect_bot/execution/broker.py:514-537).
    /\ step = "FX_STEP"
    /\ market = "US"
    /\ bootstrap_fx_req > 0
    /\ allow_fx_convert = TRUE
    /\ after_sells_snap.avail >= bootstrap_fx_req
    /\ fx_submitted'    = TRUE
    /\ fx_exchange'     = "IDEALPRO"
    /\ virtual_fx_open' = TRUE
    /\ reserve_usd'     = 0
    /\ pending_settle'  = [pending_settle EXCEPT
                            !["USD"][2] = @ - bootstrap_fx_req,
                            !["HKD"][2] = @ + (bootstrap_fx_req * FxRate)]
    /\ step'            = "COMPUTE_CAP"
    /\ UNCHANGED <<scenario, market, day, settled_cash, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, realized_sell_net, phase_b_req, bootstrap_fx_req, allow_fx_convert, spendable_cash, capped_buy_outlay>>

SkipOrReserveFx ==
    \* No live FX order submitted; reserve_usd held aside when market == "US" and not allow_fx_convert
    \* (src/fundconnect_bot/execution/policy.py:1149-1155)
    /\ step = "FX_STEP"
    /\ ~(market = "US" /\ bootstrap_fx_req > 0 /\ allow_fx_convert = TRUE /\ after_sells_snap.avail >= bootstrap_fx_req)
    /\ reserve_usd' = IF market = "US" /\ ~allow_fx_convert THEN bootstrap_fx_req ELSE 0
    /\ step'        = "COMPUTE_CAP"
    /\ UNCHANGED <<scenario, market, day, settled_cash, pending_settle, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, realized_sell_net, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, spendable_cash, capped_buy_outlay>>

\* ---- Step 4: Construct eff_cash, compute_spendable_cash & apply_cash_cap_to_buys (src/fundconnect_bot/execution/policy.py:1177-1205) ----

ComputeSpendableAndCapBuys ==
    /\ step = "COMPUTE_CAP"
    /\ LET ccy        == MarketCcy(market)
           base_total == Max2(after_sells_snap.total, pre_cash_snap.total + realized_sell_net)
           base_settl == Min2(base_total, Max2(after_sells_snap.settled, pre_cash_snap.settled + realized_sell_net))
           base_avail == Max2(after_sells_snap.avail, pre_cash_snap.avail + realized_sell_net)
           post_fx    == BrokerCashSnap(settled_cash, pending_settle, margin_bonus_usd, day, "USD")
           eff_snap   == IF ccy = "USD" /\ fx_submitted
                         THEN [
                             total   |-> Max2(0, Min2(post_fx.total, base_total - bootstrap_fx_req)),
                             settled |-> Max2(0, Min3(
                                            Max2(0, Min2(post_fx.total, base_total - bootstrap_fx_req)),
                                            post_fx.settled,
                                            base_settl - bootstrap_fx_req)),
                             avail   |-> Max2(0, Min2(post_fx.avail, base_avail - bootstrap_fx_req))
                         ]
                         ELSE [total |-> base_total, settled |-> base_settl, avail |-> base_avail]
           spend      == ComputeSpendable(ccy, eff_snap, reserve_usd)
           capped     == CapBuysOutlay(phase_b_req, spend)
       IN /\ spendable_cash'    = spend
          /\ capped_buy_outlay' = capped
          /\ step'              = "PHASE_B"
          /\ UNCHANGED <<scenario, market, day, settled_cash, pending_settle, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, realized_sell_net, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, reserve_usd>>

\* ---- Step 5: Execute capped Phase B buys (src/fundconnect_bot/execution/policy.py:1208-1214) ----

ExecPhaseBBuys ==
    /\ step = "PHASE_B"
    /\ LET ccy == MarketCcy(market)
           sd  == EquitySettleDay(market)
       IN /\ pending_settle' = IF capped_buy_outlay > 0
                               THEN [pending_settle EXCEPT ![ccy][sd] = @ - capped_buy_outlay]
                               ELSE pending_settle
          /\ step'           = "SETTLEMENT"
          /\ UNCHANGED <<scenario, market, day, settled_cash, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, realized_sell_net, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, reserve_usd, spendable_cash, capped_buy_outlay>>

\* ---- Step 6: Multi-day clearing house progression T+0 -> T+1 -> T+2 -> T+3 ----

AdvanceSettlementDay ==
    \* Credits/debits clearing obligations maturing on day + 1 into SettledCash (src/fundconnect_bot/execution/broker.py:495-500)
    /\ step = "SETTLEMENT"
    /\ day < 3
    /\ LET nxt == day + 1 IN
       /\ day'          = nxt
       /\ settled_cash' = [c \in Ccys |->
                            settled_cash[c] + (IF nxt \in SettleDays THEN pending_settle[c][nxt] ELSE 0)]
       /\ UNCHANGED <<scenario, market, step, pending_settle, margin_bonus_usd, pre_cash_snap, after_sells_snap, phase_a_req, realized_sell_net, phase_b_req, bootstrap_fx_req, allow_fx_convert, fx_submitted, fx_exchange, virtual_fx_open, reserve_usd, spendable_cash, capped_buy_outlay>>

SettlementDone ==
    /\ step = "SETTLEMENT"
    /\ day = 3
    /\ UNCHANGED vars

Next ==
    \/ ExecPhaseASellFilled
    \/ ExecPhaseASellUnfilled
    \/ RefreshCashAfterSells
    \/ ExecBootstrapFx
    \/ SkipOrReserveFx
    \/ ComputeSpendableAndCapBuys
    \/ ExecPhaseBBuys
    \/ AdvanceSettlementDay
    \/ SettlementDone

Spec == Init /\ [][Next]_vars

\* =========================================================================
\* Developer-Expectation Invariants
\* =========================================================================

TypeOK ==
    /\ scenario \in Scenarios
    /\ market \in {"US", "HK"}
    /\ step \in {"PHASE_A", "POST_SELL_CASH", "FX_STEP", "COMPUTE_CAP", "PHASE_B", "SETTLEMENT"}
    /\ day \in 0..3
    /\ fx_exchange \in {"NONE", "IDEALPRO", "FXCONV"}
    /\ spendable_cash >= 0
    /\ capped_buy_outlay >= 0

\* 1. BuyBudgetWithinCap:
\*    apply_cash_cap_to_buys guarantees Phase B buy outlay + fees never exceeds spendable_cash.
BuyBudgetWithinCap ==
    (step \in {"PHASE_B", "SETTLEMENT"}) =>
        /\ capped_buy_outlay >= 0
        /\ capped_buy_outlay <= spendable_cash

\* 2. NoUnintendedVirtualFxPosition:
\*    USD.HKD conversions must execute via FXCONV physical cash conversion rather than IDEALPRO
\*    (which opens a Virtual FX Position in TWS's FX Portfolio).
NoUnintendedVirtualFxPosition ==
    /\ ~virtual_fx_open
    /\ (fx_submitted => fx_exchange = "FXCONV")

\* 3. ZeroMarginLoanSettledCash:
\*    Across every settlement day T+0..T+3, cleared SettledCash in both USD and HKD remains >= 0
\*    (no overnight currency margin loan caused by spending unsettled proceeds or unguarded FX).
ZeroMarginLoanSettledCash ==
    \A c \in Ccys : settled_cash[c] >= 0
=============================================================================
