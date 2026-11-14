"""End-to-end test suite against the stateful `FakeTwsServer` & drop-in `ib_async` harness.

Exercises `IbAsyncBrokerAdapter`, `execute_market_rebalance`, and `cli.main(['execute', '--live'])`
across:
1. Monday US$1,000,000 100% `SGOV.US` two-phase bootstrap (`Phase A sell SGOV` -> `FX conversion`
   -> `Phase B US & HK buys`), verifying `SettledCash[USD] >= 0` and `SettledCash[HKD] >= 0`.
2. Week-2+ rotation (`3053.HK` sell funding `2800.HK` buy on SEHK `T+2`) and gap/shock scenarios
   (`BUY_STAGED_50`, `SKIP_GAP_UP`, `SKIP_ADVERSE_GAP_DOWN`, `SELL_SLICED_TWAP`,
   `HOLD_FLASH_CRASH_GUARD`, and `HALTED_OR_NO_QUOTE`).
3. Walk-limit repricing across `ib.sleep` ticks, partial fills (VWAP & Phase A partial-fill cash
   cap), wire-level `Error 104` fill-before-modify race, and async `reqTickers` fill race.
4. Failure/recovery & edge cases (`Error 110` / `Error 200` / `Error 201` -> `Inactive` rejection
   and retry, mid-rebalance crash & reconnect under same `clientId`, and cross-`clientId` / manual
   TWS GUI open-order reconciliation).
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fake_tws_server import (
    FakeIB,
    FakeTwsServer,
    Forex,
    LimitOrder,
    OrderScript,
    SettlementEntry,
    Stock,
    build_fake_ib_async_module,
)
from fundconnect_bot.cli import _synthetic_quote
from fundconnect_bot.cli import main as cli_main
from fundconnect_bot.config import (
    BOOTSTRAP_SOURCE_SYMBOL,
    HK_DEFENSIVE_SYMBOL,
    US_DEFENSIVE_SYMBOL,
    ExecutionPolicyConfig,
    IbkrConnectionConfig,
)
from fundconnect_bot.execution.broker import IbAsyncBrokerAdapter
from fundconnect_bot.execution.policy import ActionCode, execute_market_rebalance
from fundconnect_bot.strategies.composite import (
    PlanTarget,
    WeeklyPlan,
    build_weekly_plan,
    save_weekly_plan,
)

US_CACHE_DIR = Path("/home/obscure/TradeMaster/data/fundconnect_us")
HK_CACHE_DIR = Path("/home/obscure/TradeMaster/data/fundconnect_hk")


@pytest.fixture(scope="module")
def plan_20261113() -> WeeklyPlan:
    return build_weekly_plan(
        asof_date=date(2026, 11, 13),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
    )


def _make_plan(
    *,
    market: str,
    targets: list[PlanTarget],
    us_session: date = date(2026, 11, 16),
    hk_session: date = date(2026, 11, 16),
) -> WeeklyPlan:
    draft = WeeklyPlan(
        schema_id="fundconnect-ibkr-plan/v1",
        generated_at_utc="2026-11-15T00:00:00+00:00",
        asof_date=date(2026, 11, 13),
        scheduled_monday=date(2026, 11, 16),
        us_execution_session=us_session,
        us_deferred_holiday=False,
        us_open_hkt="2026-11-16T22:30:00+08:00",
        hk_execution_session=hk_session,
        hk_deferred_holiday=False,
        sleeve_weights={
            "us_macro_allweather_v1": "0.3500",
            "us_sector_quality_mom_v1": "0.3500",
            "hk_etf_southbound_rot_v1": "0.2000",
            "hk_southbound_bluechip_v1": "0.1000",
        },
        defensive_fallbacks={"US": US_DEFENSIVE_SYMBOL, "HK": HK_DEFENSIVE_SYMBOL},
        defensive_ref_closes={US_DEFENSIVE_SYMBOL: "82.93", HK_DEFENSIVE_SYMBOL: "1195.60"},
        targets=tuple(targets),
        sleeve_diagnostics={"market": market},
        provenance_digests={"mock": "0" * 64},
        summary={"target_instrument_count": str(len(targets))},
    )
    payload = draft.to_dict()
    return WeeklyPlan(**{**draft.__dict__, "plan_sha256": str(payload["plan_sha256"])})


def _install_fake_ib(monkeypatch: pytest.MonkeyPatch, server: FakeTwsServer) -> None:
    monkeypatch.setitem(sys.modules, "ib_async", build_fake_ib_async_module(server))


def _seed_quotes_for_plan(
    server: FakeTwsServer,
    plan: WeeklyPlan,
    *,
    overrides: dict[str, tuple[float, float]] | None = None,
    sgov_px: float = 100.54,
) -> None:
    ov = overrides or {}
    sgov_q = _synthetic_quote(BOOTSTRAP_SOURCE_SYMBOL, sgov_px, gap_pct=0.0, spread_bps=2.0)
    server.set_quote(
        BOOTSTRAP_SOURCE_SYMBOL,
        bid=sgov_q.bid,
        ask=sgov_q.ask,
        last=sgov_q.last,
        close=sgov_q.prev_close,
    )
    for sym, ref_str in plan.defensive_ref_closes.items():
        ref_f = float(ref_str)
        dq = _synthetic_quote(sym, ref_f, gap_pct=0.0, spread_bps=4.0)
        server.set_quote(sym, bid=dq.bid, ask=dq.ask, last=dq.last, close=dq.prev_close)
    for t in plan.targets:
        # Use narrow marketable spreads by default so initial marketable limits cross cleanly
        gap, sp = ov.get(t.symbol, (0.001, 4.0 if t.market == "US" else 8.0))
        q = _synthetic_quote(t.symbol, float(t.ref_close), gap_pct=gap, spread_bps=sp)
        server.set_quote(t.symbol, bid=q.bid, ask=q.ask, last=q.last, close=q.prev_close)


# ---------------------------------------------------------------------------
# 0. Protocol-Level Verification of FakeTwsServer Rules
# ---------------------------------------------------------------------------
def test_fake_tws_contract_and_tick_rules_error_200_error_110_and_error_201() -> None:
    server = FakeTwsServer(usd_cash=100_000.0, hkd_cash=500_000.0)
    server.set_quote("0003.HK", bid=7.38, ask=7.40, close=7.39)
    server.set_quote("0002.HK", bid=81.00, ask=81.10, close=81.05)
    server.set_quote("3053.HK", bid=1195.55, ask=1195.65, close=1195.60)
    server.set_quote("2828.HK", bid=81.52, ask=81.56, close=81.54)
    server.set_quote("AAPL.US", bid=224.98, ask=225.02, close=225.00)

    ib = FakeIB(server).connect(clientId=26)

    # 1. SEHK leading zero ('0003') rejected with Error 200; stripped '3' qualifies cleanly
    bad_sehk = Stock("0003", "SEHK", "HKD")
    assert ib.qualifyContracts(bad_sehk) == []
    assert any(e.error_code == 200 for e in server.errors)
    good_sehk = Stock("3", "SEHK", "HKD")
    assert len(ib.qualifyContracts(good_sehk)) == 1 and good_sehk.conId > 0

    # 2. Invalid HKEX Part A / Part B / Part D and US ticks rejected with Error 110 -> Inactive
    invalid_tick_cases = [
        (Stock("2", "SEHK", "HKD"), 500, 81.03),      # Part A 50..100 requires 0.05 tick
        (Stock("3053", "SEHK", "HKD"), 10, 1195.62),  # Part B 3053.HK requires 0.05 tick
        (Stock("2828", "SEHK", "HKD"), 200, 81.55),   # Part D ETP 20..100 requires 0.02 tick
        (Stock("AAPL", "SMART", "USD"), 10, 225.005), # US >= $1.00 requires 0.01 tick
    ]
    for contract, qty, bad_px in invalid_tick_cases:
        err_before = len(server.errors)
        tr = ib.placeOrder(contract, LimitOrder("BUY", qty, bad_px))
        assert tr.orderStatus.status == "Inactive"
        assert tr.isDone()
        assert server.errors[-1].error_code == 110
        assert len(server.errors) == err_before + 1

    # 3. Odd-lot SEHK BUY (150 sh vs 500 board lot) & short sale rejected with Error 201 -> Inactive
    odd_tr = ib.placeOrder(Stock("2", "SEHK", "HKD"), LimitOrder("BUY", 150, 81.05))
    assert odd_tr.orderStatus.status == "Inactive"
    assert server.errors[-1].error_code == 201

    short_tr = ib.placeOrder(Stock("AAPL", "SMART", "USD"), LimitOrder("SELL", 10, 224.98))
    assert short_tr.orderStatus.status == "Inactive"
    assert server.errors[-1].error_code == 201
    ib.disconnect()


def test_fake_tws_fx_idealpro_vs_fxconv_virtual_position_and_t2_settlement() -> None:
    # A: IDEALPRO (< $25k triggers odd-lot Warning 399 AND opens Virtual FX Position)
    s_ideal = FakeTwsServer(usd_cash=50_000.0, hkd_cash=0.0, usdhkd_bid=7.8000, usdhkd_ask=7.8010)
    ib_a = FakeIB(s_ideal).connect(clientId=1)
    tr_a = ib_a.placeOrder(Forex("USDHKD", exchange="IDEALPRO"), LimitOrder("SELL", 10_000, 7.7995))
    assert tr_a.orderStatus.status == "Filled"
    assert any(w.error_code == 399 for w in s_ideal.warnings)
    assert s_ideal.virtual_fx_usd_position == pytest.approx(-10_000.0)
    assert any(getattr(it.contract, "secType", "") == "CASH" for it in ib_a.portfolio())
    # T+0: CashBalance(HKD) updated, SettledCash(HKD) remains 0.0 until T+2!
    assert s_ideal.cash_balance["HKD"] == pytest.approx(78_000.0)
    assert s_ideal.settled_cash("HKD") == pytest.approx(0.0)
    s_ideal.advance_settlement_days(1)
    assert s_ideal.settled_cash("HKD") == pytest.approx(0.0)
    s_ideal.advance_settlement_days(1)
    assert s_ideal.settled_cash("HKD") == pytest.approx(78_000.0)
    ib_a.disconnect()

    # B: FXCONV performs pure physical conversion with ZERO Virtual FX Position
    s_conv = FakeTwsServer(usd_cash=50_000.0, hkd_cash=0.0, usdhkd_bid=7.8000, usdhkd_ask=7.8010)
    ib_b = FakeIB(s_conv).connect(clientId=1)
    tr_b = ib_b.placeOrder(Forex("USDHKD", exchange="FXCONV"), LimitOrder("SELL", 30_000, 7.7995))
    assert tr_b.orderStatus.status == "Filled"
    assert s_conv.virtual_fx_usd_position == pytest.approx(0.0)
    assert all(getattr(it.contract, "secType", "") != "CASH" for it in ib_b.portfolio())
    assert s_conv.settled_cash("HKD") == pytest.approx(0.0)
    s_conv.advance_settlement_days(2)
    assert s_conv.settled_cash("USD") == pytest.approx(20_000.0)
    assert s_conv.settled_cash("HKD") == pytest.approx(234_000.0)
    ib_b.disconnect()


# ---------------------------------------------------------------------------
# 1. Monday US$1,000,000 100% SGOV.US Two-Phase Bootstrap E2E
# ---------------------------------------------------------------------------
def test_e2e_cli_live_monday_1m_sgov_staged_zero_margin_bootstrap(
    plan_20261113: WeeklyPlan,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = save_weekly_plan(plan_20261113, tmp_path / "plan_20261113.json")
    state_dir = tmp_path / "state"

    sgov_shares = 9_946
    sgov_px = 100.54
    init_usd = 1_000_000.0 - (sgov_shares * sgov_px)  # $29.16

    server = FakeTwsServer(
        base_currency="USD",
        usd_cash=init_usd,
        usd_settled_cash=init_usd,
        hkd_cash=0.0,
        hkd_settled_cash=0.0,
        usdhkd_bid=7.8000,
        usdhkd_ask=7.8004,
        fill_mode="MID_OR_BETTER",
    )
    server.set_position(BOOTSTRAP_SOURCE_SYMBOL, sgov_shares, sgov_px, sgov_px)
    _seed_quotes_for_plan(server, plan_20261113, sgov_px=sgov_px)
    _install_fake_ib(monkeypatch, server)

    # --- Step 1 (Monday day=0): Sell 9,946 SGOV.US (T+1), buy 68.25% US sleeve (T+1), reserve $300k ---
    rc_us_mon = cli_main(
        [
            "execute",
            "--plan",
            str(plan_path),
            "--market",
            "US",
            "--bootstrap-fx-usd",
            "300000",
            "--live",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert rc_us_mon == 0
    assert BOOTSTRAP_SOURCE_SYMBOL not in server.holdings
    assert len(server.errors) == 0
    # At Monday EOD: SGOV sale (+~$999.9k) and US buys (-~$682k) both settle on day=1 -> SettledCash >= 0!
    assert server.settled_cash("USD") >= 0.0
    assert server.settled_cash("HKD") == pytest.approx(0.0)
    us_orders_after_mon = len(server.orders)
    assert us_orders_after_mon == 13  # 1 SGOV sell + 12 US target buys

    # --- Step 2 (Tuesday day=1): T+1 US settlement completes -> Convert $300,000 USD to HKD ---
    server.advance_settlement_days(1)
    assert server.settled_cash("USD") > 315_000.0

    rc_fx_tue = cli_main(
        [
            "execute",
            "--plan",
            str(plan_path),
            "--market",
            "US",
            "--bootstrap-fx-usd",
            "300000",
            "--allow-fx-convert",
            "--live",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert rc_fx_tue == 0
    # Zero duplicate US equity orders placed; only 1 FX order added!
    assert len(server.orders) == us_orders_after_mon + 1
    assert server.settled_cash("USD") >= 15_000.0
    assert server.cash_balance["HKD"] == pytest.approx(2_340_000.0)
    # T+2 FX has NOT settled yet on Tuesday (day=1): SettledCash(HKD) == 0.0!
    assert server.settled_cash("HKD") == pytest.approx(0.0)

    # Premature HK execution on Tuesday (before T+2 FX settlement) MUST block all HK buys!
    orders_before_premature_hk = len(server.orders)
    rc_hk_early = cli_main(
        [
            "execute",
            "--plan",
            str(plan_path),
            "--market",
            "HK",
            "--live",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert rc_hk_early == 0
    assert len(server.orders) == orders_before_premature_hk
    assert server.settled_cash("HKD") == pytest.approx(0.0)

    # --- Step 3 (Thursday day=3): T+2 FX settles -> Execute HK sleeve with 0 margin interest ---
    server.advance_settlement_days(2)
    assert server.settled_cash("HKD") == pytest.approx(2_340_000.0)

    rc_hk_thu = cli_main(
        [
            "execute",
            "--plan",
            str(plan_path),
            "--market",
            "HK",
            "--live",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert rc_hk_thu == 0
    assert len(server.errors) == 0  # Zero Error 110 (ticks) and zero Error 200 (SEHK symbols)
    assert "3053.HK" in server.holdings
    assert server.settled_cash("USD") >= 0.0
    assert server.settled_cash("HKD") >= 0.0

    # Verify board-lot compliance on every held HK instrument
    for t in plan_20261113.targets_for_market("HK"):
        if t.symbol in server.holdings:
            assert server.holdings[t.symbol].quantity % t.lot_size == 0

    # Advance through HK T+2 CCASS settlement (day=5) and verify SettledCash stays >= 0 throughout
    for _ in range(2):
        server.advance_settlement_days(1)
        assert server.settled_cash("USD") >= 0.0
        assert server.settled_cash("HKD") >= 0.0

    # Re-running HK CLI immediately is 100% idempotent (0 additional orders)
    orders_after_hk = len(server.orders)
    assert (
        cli_main(
            [
                "execute",
                "--plan",
                str(plan_path),
                "--market",
                "HK",
                "--live",
                "--state-dir",
                str(state_dir),
            ]
        )
        == 0
    )
    assert len(server.orders) == orders_after_hk


def test_e2e_hkd_base_account_same_day_sgov_sell_fx_and_t2_hk_rebalance(
    plan_20261113: WeeklyPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify bootstrap on an HKD-base account (`AvailableFunds` emitted ONLY in HKD)."""
    sgov_shares = 9_946
    sgov_px = 100.54
    init_usd = 1_000_000.0 - (sgov_shares * sgov_px)

    server = FakeTwsServer(
        base_currency="HKD",
        usd_cash=init_usd,
        usd_settled_cash=init_usd,
        hkd_cash=0.0,
        hkd_settled_cash=0.0,
        usdhkd_bid=7.8000,
        usdhkd_ask=7.8004,
        fill_mode="MID_OR_BETTER",
    )
    server.set_position(BOOTSTRAP_SOURCE_SYMBOL, sgov_shares, sgov_px, sgov_px)
    _seed_quotes_for_plan(server, plan_20261113, sgov_px=sgov_px)
    _install_fake_ib(monkeypatch, server)

    cfg = ExecutionPolicyConfig(poll_interval_sec=0.0)
    with IbAsyncBrokerAdapter() as adapter:
        us_rep = execute_market_rebalance(
            plan_20261113,
            adapter,
            market="US",
            dry_run=False,
            bootstrap_fx_usd=300_000.0,
            allow_fx_convert=True,
            config=cfg,
        )
        assert len(us_rep.phase_a_fills) == 1 and us_rep.phase_a_fills[0].status == "FILLED"
        assert us_rep.fx_report is not None and us_rep.fx_report.status == "Filled"
        assert all(f.status == "FILLED" for f in us_rep.phase_b_fills)
        assert server.settled_cash("USD") >= 0.0
        assert server.settled_cash("HKD") == pytest.approx(0.0)

        # Day 1 (T+1): US settles; HKD FX still unsettled (T+2)
        server.advance_settlement_days(1)
        assert server.settled_cash("USD") >= 0.0
        assert server.settled_cash("HKD") == pytest.approx(0.0)

        # Day 2 (T+2): FX settles -> execute HK buys
        server.advance_settlement_days(1)
        assert server.settled_cash("HKD") == pytest.approx(2_340_000.0)
        hk_rep = execute_market_rebalance(
            plan_20261113,
            adapter,
            market="HK",
            dry_run=False,
            config=cfg,
        )
        assert len(hk_rep.phase_b_fills) >= 6
        assert all(f.status == "FILLED" for f in hk_rep.phase_b_fills)
        assert server.settled_cash("USD") >= 0.0
        assert server.settled_cash("HKD") >= 0.0


def test_e2e_fx_conversion_routes_fxconv_without_virtual_fx_or_duplicate_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    us_plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="SHY.US",
                ibkr_symbol="SHY",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="etf",
                portfolio_weight=Decimal("0.50000000"),
                sleeve_contributions={"us_macro_allweather_v1": "0.50000000"},
                ref_close=Decimal("82.93"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=True,
            )
        ],
    )
    server = FakeTwsServer(
        usd_cash=500_000.0,
        usd_settled_cash=500_000.0,
        usdhkd_bid=7.8000,
        usdhkd_ask=7.8004,
    )
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    _install_fake_ib(monkeypatch, server)

    cfg = ExecutionPolicyConfig(poll_interval_sec=0.0)
    with IbAsyncBrokerAdapter() as adapter:
        rep1 = execute_market_rebalance(
            us_plan,
            adapter,
            market="US",
            dry_run=False,
            bootstrap_fx_usd=100_000.0,
            allow_fx_convert=True,
            state_dir=tmp_path,
            config=cfg,
        )
        assert rep1.fx_report is not None
        # Physical conversion should use FXCONV -> zero Virtual FX Position!
        assert server.virtual_fx_usd_position == pytest.approx(0.0)

        # Re-running the exact same command with persisted state must NOT convert another $100k!
        rep2 = execute_market_rebalance(
            us_plan,
            adapter,
            market="US",
            dry_run=False,
            bootstrap_fx_usd=100_000.0,
            allow_fx_convert=True,
            state_dir=tmp_path,
            config=cfg,
        )
        assert rep2.fx_report is None or rep2.fx_report.status == "ALREADY_COMPLETED"
        assert server.cash_balance["HKD"] == pytest.approx(780_000.0)


# ---------------------------------------------------------------------------
# 2. Week-2+ SEHK T+2 Rotation & Gap/Shock Scenarios E2E
# ---------------------------------------------------------------------------
def test_e2e_week2_hk_rotation_3053_sell_funds_2800_buy_sehk_t2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rot_plan = _make_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="2800.HK",
                ibkr_symbol="2800",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="etf",
                portfolio_weight=Decimal("0.18000000"),
                sleeve_contributions={"hk_etf_southbound_rot_v1": "0.18000000"},
                ref_close=Decimal("21.20"),
                lot_size=500,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="0002.HK",
                ibkr_symbol="2",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.10000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.10000000"},
                ref_close=Decimal("81.05"),
                lot_size=500,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
        ],
    )
    # USD-base account: 1,950 sh 3053.HK (~HK$2.331M), HK$150 settled HKD, plus HK$1,000,000
    # unsettled FX settling on day=3 (1 day AFTER today's SEHK T+2 trades on day=2!).
    server = FakeTwsServer(
        base_currency="USD",
        usd_cash=600_000.0,
        usd_settled_cash=600_000.0,
        hkd_cash=150.0,
        hkd_settled_cash=150.0,
    )
    # Inject HK$1,000,000 unsettled FX arriving at day=3 so day=2 SEHK buys CANNOT touch it
    server.cash_balance["HKD"] += 1_000_000.0
    server.settlement_ledger.append(
        SettlementEntry(settle_day=3, currency="HKD", amount=1_000_000.0, description="unsettled_fx_d3")
    )
    server.set_position("3053.HK", 1_950, 1190.0, 1195.60)
    server.set_position("SHY.US", 1_000, 82.90, 82.93)
    server.set_quote("3053.HK", bid=1195.55, ask=1195.65, close=1195.60)
    server.set_quote("2800.HK", bid=21.20, ask=21.22, close=21.20)
    server.set_quote("0002.HK", bid=81.00, ask=81.05, close=81.05)
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            rot_plan,
            adapter,
            market="HK",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.0),
        )

    assert len(rep.phase_a_fills) == 1
    assert rep.phase_a_fills[0].symbol == "3053.HK"
    assert rep.phase_a_fills[0].status == "FILLED"
    # Phase B spent ONLY (150 settled + ~2.331M 3053.HK T+2 sale), leaving HK$1,000,000 unsettled FX untouched!
    assert 2_325_000.0 < rep.spendable_cash_before_buys < 2_335_000.0
    assert {f.symbol for f in rep.phase_b_fills} == {"2800.HK", "0002.HK"}
    assert all(f.status == "FILLED" for f in rep.phase_b_fills)
    assert server.holdings["2800.HK"].quantity % 500 == 0
    assert server.holdings["0002.HK"].quantity % 500 == 0
    assert len(server.errors) == 0

    # Verify SettledCash(HKD) >= 0 on day 0, day 1, day 2 (SEHK T+2), and day 3 (FX settles)!
    assert server.settled_cash("HKD") >= 0.0
    for _ in range(3):
        server.advance_settlement_days(1)
        assert server.settled_cash("HKD") >= 0.0


def test_e2e_gap_and_shock_matrix_us_and_hk(monkeypatch: pytest.MonkeyPatch) -> None:
    # A. US Gap Matrix: BUY_STAGED_50 (+4.5%), SKIP_GAP_UP (+8.0%), SKIP_ADVERSE_GAP_DOWN (-4.8%)
    us_plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="LLY.US",
                ibkr_symbol="LLY",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.20000000"},
                ref_close=Decimal("800.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.20000000"},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="DIS.US",
                ibkr_symbol="DIS",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.20000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
        ],
    )
    s_us = FakeTwsServer(usd_cash=100_000.0, usd_settled_cash=100_000.0, fill_mode="MID_OR_BETTER")
    s_us.set_quote("LLY.US", bid=835.80, ask=836.20, close=800.00)   # +4.5% -> BUY_STAGED_50
    s_us.set_quote("AAPL.US", bid=215.95, ask=216.05, close=200.00)  # +8.0% -> SKIP_GAP_UP
    s_us.set_quote("DIS.US", bid=95.15, ask=95.25, close=100.00)     # -4.8% -> SKIP_ADVERSE_GAP_DOWN
    s_us.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    _install_fake_ib(monkeypatch, s_us)

    with IbAsyncBrokerAdapter() as adapter:
        rep_us = execute_market_rebalance(
            us_plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.0),
        )
    dec_us = {d.symbol: d for d in rep_us.batch.decisions}
    assert dec_us["LLY.US"].action == ActionCode.BUY_STAGED_50
    assert dec_us["LLY.US"].order_style == "ADAPTIVE_PATIENT"
    assert dec_us["AAPL.US"].action == ActionCode.SKIP_GAP_UP
    assert dec_us["DIS.US"].action == ActionCode.SKIP_ADVERSE_GAP_DOWN
    assert dec_us["SHY.US"].action == ActionCode.BUY_DEFENSIVE_FALLBACK
    # 50% of LLY ($10k) + 100% of AAPL ($20k) + 100% of DIS ($20k) = $50k redirected to SHY.US!
    assert rep_us.batch.redirected_defensive_notional == pytest.approx(50_000.0)
    assert {f.symbol for f in rep_us.phase_b_fills} == {"LLY.US", "SHY.US"}
    assert "AAPL.US" not in s_us.holdings and "DIS.US" not in s_us.holdings

    # B. HK Sell Severe Gap (-7.5% -> 3-slice TWAP), Flash-Crash (-15% -> floor limit), & Halted Quote
    hk_plan = _make_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="0669.HK",
                ibkr_symbol="669",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.20000000"},
                ref_close=Decimal("100.00"),
                lot_size=100,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="9999.HK",
                ibkr_symbol="9999",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.20000000"},
                ref_close=Decimal("190.00"),
                lot_size=100,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
        ],
    )
    s_hk = FakeTwsServer(hkd_cash=300_000.0, hkd_settled_cash=300_000.0, fill_mode="MID_OR_BETTER")
    s_hk.set_position("0003.HK", 6_000, 8.00, 8.00)  # Not in targets -> full exit at -7.5% gap!
    s_hk.set_position("0002.HK", 1_000, 100.00, 100.00)  # Not in targets -> flash crash -15.0%!
    s_hk.set_quote("0003.HK", bid=7.39, ask=7.41, close=8.00)     # -7.5% -> 3-slice TWAP
    s_hk.set_quote("0002.HK", bid=84.95, ask=85.05, close=100.00)  # -15.0% -> PROTECTIVE_FLOOR_LIMIT @ 88.00
    s_hk.set_quote("0669.HK", bid=107.40, ask=107.60, close=100.00)  # +7.5% -> SKIP_GAP_UP -> 3053.HK
    s_hk.set_quote("9999.HK", bid=0.0, ask=0.0, close=190.00)        # Halted -> HALTED_OR_NO_QUOTE
    s_hk.set_quote("3053.HK", bid=1195.55, ask=1195.65, close=1195.60)
    _install_fake_ib(monkeypatch, s_hk)

    with IbAsyncBrokerAdapter() as adapter:
        rep_hk = execute_market_rebalance(
            hk_plan,
            adapter,
            market="HK",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.0),
        )
    dec_hk = {d.symbol: d for d in rep_hk.batch.decisions}
    assert dec_hk["0003.HK"].action == ActionCode.SELL_SLICED_TWAP
    assert dec_hk["0003.HK"].slice_quantities == (2000, 2000, 2000)
    assert dec_hk["0002.HK"].action == ActionCode.HOLD_FLASH_CRASH_GUARD
    assert dec_hk["0002.HK"].order_style == "PROTECTIVE_FLOOR_LIMIT"
    assert dec_hk["0002.HK"].limit_price == Decimal("88.00")
    assert dec_hk["0669.HK"].action == ActionCode.SKIP_GAP_UP
    assert dec_hk["9999.HK"].action == ActionCode.HALTED_OR_NO_QUOTE
    assert dec_hk["3053.HK"].action == ActionCode.BUY_DEFENSIVE_FALLBACK

    # 0002.HK protective floor @ 88.00 rests unfilled above 84.95 bid (never dumped into air pocket!)
    floor_fills = [f for f in rep_hk.phase_a_fills if f.symbol == "0002.HK"]
    assert len(floor_fills) == 1 and floor_fills[0].status == "WORKING" and floor_fills[0].reprice_count == 0
    assert s_hk.holdings["0002.HK"].quantity == 1_000


# ---------------------------------------------------------------------------
# 3. Walk-Limit Repricing, Partial Fills & Async Modify Races (Error 104)
# ---------------------------------------------------------------------------
def test_e2e_walk_limit_repricing_across_sleep_steps_and_hard_slippage_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.40000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.40000000"},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="MSFT.US",
                ibkr_symbol="MSFT",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.40000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.40000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
        ],
    )
    server = FakeTwsServer(usd_cash=50_000.0, usd_settled_cash=50_000.0, fill_mode="CROSS_SPREAD")
    # AAPL.US: mid=200.00, ask=200.38 (+19 bps, spread=38 > 35 initially? Use bid=199.64, ask=200.36 -> mid=200.00, spread=36? Let's use bid=199.66, ask=200.34 -> mid=200.00, spread=34 bps <= 35 bps, ask=+17 bps!)
    # Step 0: +8 bps = 200.16 (< 200.34 -> WORKING)
    # Step 1: +14 bps = 200.28 (< 200.34 -> WORKING)
    # Step 2: +20 bps = 200.40 -> capped at ask=200.34 -> CROSSES ask & FILLS on reprice #2!
    server.set_quote("AAPL.US", bid=199.66, ask=200.34, close=200.00)
    # MSFT.US: starts wide spread (bid=99.60, ask=100.40 -> 80 bps -> passive mid 100.00), then spread tightens to
    # bid=100.05, ask=100.35 (29.9 bps, ask=+35 bps above arrival_mid=100.00) on first sleep so walk steps run up to +25 bps (100.25) and STOP!
    server.set_quote("MSFT.US", bid=99.60, ask=100.40, close=100.00)
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)

    def _tighten_msft_book(srv: FakeTwsServer, _sec: float) -> None:
        if any(r.canon_symbol == "MSFT.US" for r in srv.orders):
            srv.set_quote("MSFT.US", bid=100.05, ask=100.35, close=100.00)

    server.sleep_hooks.append(_tighten_msft_book)
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=1.0),
        )

    by_sym = {f.symbol: f for f in rep.phase_b_fills}
    assert by_sym["AAPL.US"].status == "FILLED"
    assert by_sym["AAPL.US"].reprice_count == 2
    assert by_sym["AAPL.US"].final_limit_price == Decimal("200.34")
    assert by_sym["AAPL.US"].avg_fill_price == pytest.approx(200.34)

    # MSFT.US walked 3 times (100.14 -> 100.20 -> 100.25) and respected the +25 bps hard cap!
    assert by_sym["MSFT.US"].status == "WORKING"
    assert by_sym["MSFT.US"].reprice_count == 3
    assert by_sym["MSFT.US"].final_limit_price == Decimal("100.25")


def test_e2e_partial_fills_vwap_and_phase_a_partial_sell_cash_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sub-case A: Partial fill on initial submit (status='Submitted', filled=40, rem=60) -> walk fills remaining 60
    buy_plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.50000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.50000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            )
        ],
    )
    s_buy = FakeTwsServer(usd_cash=20_000.0, usd_settled_cash=20_000.0, fill_mode="CROSS_SPREAD")
    s_buy.set_quote("AAPL.US", bid=99.96, ask=100.04, close=100.00)
    s_buy.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    # Eval 0 (placeOrder): fill 40 sh @ 100.04; Eval 1 (ib.sleep(0.5) inside place_order): keep 40 sh, lift ask to 100.12;
    # Eval 2 (poll_order sleep): still < 100.12; Eval 3 (modify to 100.12): fill remaining 60 sh @ 100.12!
    s_buy.set_order_script("AAPL.US", OrderScript(max_cum_fill_by_eval={0: 40, 1: 40, 2: 40, 3: 100}))

    def _lift_ask_after_partial(srv: FakeTwsServer, _sec: float) -> None:
        rec = next((r for r in srv.orders if r.canon_symbol == "AAPL.US"), None)
        if rec is not None and round(rec.filled) == 40:
            srv.set_quote("AAPL.US", bid=100.00, ask=100.12, close=100.00)

    s_buy.sleep_hooks.append(_lift_ask_after_partial)
    _install_fake_ib(monkeypatch, s_buy)

    with IbAsyncBrokerAdapter() as adapter:
        rep_a = execute_market_rebalance(
            buy_plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.5),
        )
    fill_a = rep_a.phase_b_fills[0]
    assert fill_a.status == "FILLED"
    assert fill_a.filled_qty == 100
    assert fill_a.reprice_count == 1
    expected_vwap = (40 * 100.04 + 60 * 100.12) / 100.0
    assert fill_a.avg_fill_price == pytest.approx(expected_vwap)

    # Sub-case B: Phase A SGOV.US sell partially fills ONLY 400 / 1,000 shares -> Phase B caps buys to ~$40k!
    s_part_sell = FakeTwsServer(usd_cash=100.0, usd_settled_cash=100.0, fill_mode="CROSS_SPREAD")
    s_part_sell.set_position("SGOV.US", 1_000, 100.00, 100.00)
    s_part_sell.set_quote("SGOV.US", bid=99.99, ask=100.01, close=100.00)
    s_part_sell.set_quote("AAPL.US", bid=99.98, ask=100.02, close=100.00)
    s_part_sell.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    # SGOV.US fills 400 sh across all evals, leaving 600 sh unfilled (PARTIAL)
    s_part_sell.set_order_script("SGOV.US", OrderScript(max_cum_fill_by_eval={0: 400}))
    _install_fake_ib(monkeypatch, s_part_sell)

    with IbAsyncBrokerAdapter() as adapter:
        rep_b = execute_market_rebalance(
            buy_plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    assert rep_b.phase_a_fills[0].status == "PARTIAL"
    assert rep_b.phase_a_fills[0].filled_qty == 400
    assert 39_900.0 < rep_b.spendable_cash_before_buys < 40_200.0
    assert rep_b.phase_b_fills[0].filled_qty <= 400
    assert s_part_sell.settled_cash("USD") >= 0.0


def test_e2e_wire_race_fill_on_modify_error_104(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server-side wire race: order fills on venue right before modify packet arrives (`Error 104`)."""
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.40000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.40000000"},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            )
        ],
    )
    server = FakeTwsServer(usd_cash=50_000.0, usd_settled_cash=50_000.0, fill_mode="CROSS_SPREAD")
    server.set_quote("AAPL.US", bid=199.66, ask=200.34, close=200.00)
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    server.set_order_script("AAPL.US", OrderScript(wire_fill_race_on_modify=True))
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )

    assert any(e.error_code == 104 for e in server.errors)
    fill = rep.phase_b_fills[0]
    assert fill.status == "FILLED"
    # Filled at the original working limit (200.16), NOT the rejected modify limit (200.28)!
    assert fill.avg_fill_price == pytest.approx(200.16)
    assert fill.final_limit_price == Decimal("200.16")
    assert fill.reprice_count == 0


def test_e2e_async_fill_during_get_quotes_before_modify_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.40000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.40000000"},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            )
        ],
    )
    server = FakeTwsServer(usd_cash=50_000.0, usd_settled_cash=50_000.0, fill_mode="CROSS_SPREAD")
    server.set_quote("AAPL.US", bid=199.66, ask=200.34, close=200.00)
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    server.set_order_script("AAPL.US", OrderScript(async_fill_on_req_tickers=True))
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    fill = rep.phase_b_fills[0]
    assert fill.status == "FILLED"
    assert fill.reprice_count == 0
    assert fill.final_limit_price == Decimal("200.16")


# ---------------------------------------------------------------------------
# 4. Failure/Recovery, Error 110 Rejection Retry, Crash/Restart & Cross-clientId
# ---------------------------------------------------------------------------
def test_e2e_error_110_inactive_rejection_and_clean_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hk_plan = _make_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="0002.HK",
                ibkr_symbol="2",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.15000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.15000000"},
                ref_close=Decimal("81.05"),
                lot_size=500,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="3053.HK",
                ibkr_symbol="3053",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="etf",
                portfolio_weight=Decimal("0.15000000"),
                sleeve_contributions={"hk_etf_southbound_rot_v1": "0.15000000"},
                ref_close=Decimal("1195.60"),
                lot_size=1,
                defensive_fallback="3053.HK",
                is_defensive_fallback=True,
            ),
        ],
    )
    plan_path = save_weekly_plan(hk_plan, tmp_path / "plan_hk.json")
    state_dir = tmp_path / "state"

    server = FakeTwsServer(hkd_cash=1_000_000.0, hkd_settled_cash=1_000_000.0)
    server.set_quote("0002.HK", bid=81.00, ask=81.05, close=81.05)
    server.set_quote("3053.HK", bid=1195.55, ask=1195.60, close=1195.60)
    # Inject Error 110 -> Inactive rejection on 0002.HK during Run 1
    server.set_order_script(
        "0002.HK",
        OrderScript(
            reject_on_place=(
                110,
                "The price does not conform to the minimum price variation for this contract.",
                "Inactive",
            )
        ),
    )
    _install_fake_ib(monkeypatch, server)

    assert (
        cli_main(
            [
                "execute",
                "--plan",
                str(plan_path),
                "--market",
                "HK",
                "--live",
                "--state-dir",
                str(state_dir),
            ]
        )
        == 0
    )
    assert "0002.HK" not in server.holdings
    assert "3053.HK" in server.holdings
    qty_3053_after_run1 = server.holdings["3053.HK"].quantity

    # Clear the transient Error 110 fault and retry: 3053.HK must be skipped, 0002.HK must execute!
    server.scripts.pop("0002.HK", None)
    assert (
        cli_main(
            [
                "execute",
                "--plan",
                str(plan_path),
                "--market",
                "HK",
                "--live",
                "--state-dir",
                str(state_dir),
            ]
        )
        == 0
    )
    assert "0002.HK" in server.holdings and server.holdings["0002.HK"].quantity > 0
    assert server.holdings["3053.HK"].quantity == qty_3053_after_run1


def test_e2e_crash_mid_rebalance_and_reconnect_same_client_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.30000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.30000000"},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="MSFT.US",
                ibkr_symbol="MSFT",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.30000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.30000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
        ],
    )
    server = FakeTwsServer(usd_cash=100_000.0, usd_settled_cash=100_000.0, fill_mode="CROSS_SPREAD")
    # AAPL.US fills immediately; MSFT.US rests working and disconnects socket on poll BEFORE state saves!
    server.set_quote("AAPL.US", bid=199.98, ask=200.02, close=200.00)
    server.set_quote("MSFT.US", bid=99.66, ask=100.34, close=100.00)
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    server.set_order_script("MSFT.US", OrderScript(disconnect_on_poll=True))
    _install_fake_ib(monkeypatch, server)

    with pytest.raises(ConnectionError, match="Simulated TWS socket drop"), IbAsyncBrokerAdapter(
        IbkrConnectionConfig(client_id=26)
    ) as adapter1:
        execute_market_rebalance(
            plan,
            adapter1,
            market="US",
            dry_run=False,
            state_dir=tmp_path,
            config=ExecutionPolicyConfig(poll_interval_sec=1.0),
        )

    assert len(server.orders) == 2
    assert "AAPL.US" in server.holdings

    # Reconnect with the SAME clientId=26: AAPL is in positions(), MSFT is in openTrades() -> 0 new orders!
    with IbAsyncBrokerAdapter(IbkrConnectionConfig(client_id=26)) as adapter2:
        rep2 = execute_market_rebalance(
            plan,
            adapter2,
            market="US",
            dry_run=False,
            state_dir=tmp_path,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    assert len(rep2.phase_b_fills) == 0
    assert len(server.orders) == 2


def test_e2e_cross_client_id_and_manual_tws_open_orders_prevent_double_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.20000000"},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="MSFT.US",
                ibkr_symbol="MSFT",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.20000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.20000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
        ],
    )
    server = FakeTwsServer(usd_cash=100_000.0, usd_settled_cash=100_000.0, fill_mode="CROSS_SPREAD")
    server.set_quote("AAPL.US", bid=199.66, ask=200.34, close=200.00)
    server.set_quote("MSFT.US", bid=99.66, ask=100.34, close=100.00)
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)

    # 1. Working order placed by prior crashed session clientId=26 (100 sh AAPL.US)
    ib_old = FakeIB(server).connect(clientId=26)
    ib_old.placeOrder(Stock("AAPL", "SMART", "USD"), LimitOrder("BUY", 100, 200.16))
    ib_old.disconnect()

    # 2. Manual working order placed via TWS desktop UI (clientId=-1, 200 sh MSFT.US)
    server.inject_manual_tws_order("MSFT.US", "BUY", 200, 100.08)
    assert len(server.orders) == 2

    # 3. Reconnect under failover clientId=27: must see both open orders and submit ZERO duplicates!
    _install_fake_ib(monkeypatch, server)
    with IbAsyncBrokerAdapter(IbkrConnectionConfig(client_id=27)) as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            state_dir=tmp_path,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    assert len(rep.phase_b_fills) == 0
    assert len(server.orders) == 2


def test_e2e_unfilled_working_order_cancelled_on_tws_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="0002.HK",
                ibkr_symbol="2",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.40000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.40000000"},
                ref_close=Decimal("81.05"),
                lot_size=500,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            )
        ],
    )
    server = FakeTwsServer(
        hkd_cash=500_000.0, hkd_settled_cash=500_000.0, fill_mode="CROSS_SPREAD"
    )
    # Run 1: wide spread (80.60 / 81.50 -> ~111 bps) places passive mid 81.05, stays WORKING (0 filled)
    server.set_quote("0002.HK", bid=80.60, ask=81.50, close=81.05)
    server.set_quote("3053.HK", bid=1195.55, ask=1195.65, close=1195.60)
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter1:
        rep1 = execute_market_rebalance(
            plan,
            adapter1,
            market="HK",
            dry_run=False,
            state_dir=tmp_path,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    assert rep1.phase_b_fills[0].status == "WORKING"
    assert "0002.HK" not in server.holdings

    # Working order is cancelled/expired on TWS, and spread normalizes to 81.00 / 81.05
    server.cancel_server_order(server.orders[0].perm_id, status="Cancelled")
    server.set_quote("0002.HK", bid=81.00, ask=81.05, close=81.05)

    # Run 2: 0 shares held and 0 open orders working -> must retry and fill 0002.HK!
    with IbAsyncBrokerAdapter() as adapter2:
        rep2 = execute_market_rebalance(
            plan,
            adapter2,
            market="HK",
            dry_run=False,
            state_dir=tmp_path,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    assert len(rep2.phase_b_fills) == 1 and rep2.phase_b_fills[0].status == "FILLED"
    assert "0002.HK" in server.holdings and server.holdings["0002.HK"].quantity > 0


def test_e2e_hkex_coarse_tick_walk_limit_reaches_ask_on_later_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="3988.HK",
                ibkr_symbol="3988",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.40000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.40000000"},
                ref_close=Decimal("6.28"),
                lot_size=1000,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            )
        ],
    )
    server = FakeTwsServer(
        hkd_cash=200_000.0, hkd_settled_cash=200_000.0, fill_mode="CROSS_SPREAD"
    )
    # 3988.HK: bid=6.27, ask=6.29 -> mid=6.28, spread=31.8 bps <= 60 bps.
    # Initial +8 bps -> 6.285 -> floors to 6.28 (< ask 6.29).
    # Step 1 (+14 bps) -> 6.2888 -> floors to 6.28 (unchanged), Step 2 (+20 bps) -> 6.2925 -> 6.29 == ask!
    server.set_quote("3988.HK", bid=6.27, ask=6.29, close=6.28)
    server.set_quote("3053.HK", bid=1195.55, ask=1195.65, close=1195.60)
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="HK",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    fill = rep.phase_b_fills[0]
    assert fill.status == "FILLED"
    assert fill.final_limit_price == Decimal("6.29")
    assert fill.reprice_count == 1


def test_e2e_async_fill_and_cancel_during_get_quotes_with_wide_spread_no_stale_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (`L1-B`): async fill/cancel arriving during `get_quotes()` when `new_lmt is None`."""
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.50000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.50000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            )
        ],
    )
    # Sub-case A: Phase A SGOV.US starts wide-spread (60 bps > 35 bps -> new_lmt is None) and
    # fills asynchronously inside reqTickers() during walk step 1; Phase B AAPL.US is wide-spread
    # and cancels asynchronously inside reqTickers().
    server = FakeTwsServer(usd_cash=50.0, usd_settled_cash=50.0, fill_mode="CROSS_SPREAD")
    server.set_position("SGOV.US", 1_000, 100.00, 100.00)
    server.set_quote("SGOV.US", bid=99.70, ask=100.30, close=100.00)  # 60 bps wide spread
    server.set_quote("AAPL.US", bid=99.70, ask=100.30, close=100.00)  # 60 bps wide spread
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    server.set_order_script("SGOV.US", OrderScript(async_fill_on_req_tickers=True))
    server.set_order_script("AAPL.US", OrderScript(async_cancel_on_req_tickers=True))
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )

    assert len(rep.phase_a_fills) == 1
    assert rep.phase_a_fills[0].status == "FILLED"
    assert rep.phase_a_fills[0].filled_qty == 1_000
    assert rep.spendable_cash_before_buys > 99_900.0
    assert len(rep.phase_b_fills) == 1
    assert rep.phase_b_fills[0].status == "CANCELLED"


def test_e2e_zero_margin_fx_guard_blocks_fx_when_phase_a_sgov_unfilled_on_margin_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (`L2-B2`): `allow_fx_convert=True` must not borrow USD on Margin when Phase A stays WORKING."""
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="SHY.US",
                ibkr_symbol="SHY",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="etf",
                portfolio_weight=Decimal("0.60000000"),
                sleeve_contributions={"us_macro_allweather_v1": "0.60000000"},
                ref_close=Decimal("82.93"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=True,
            )
        ],
    )
    server = FakeTwsServer(
        base_currency="USD",
        account_type="MARGIN",
        usd_cash=100.0,
        usd_settled_cash=100.0,
        hkd_cash=0.0,
        hkd_settled_cash=0.0,
        fill_mode="CROSS_SPREAD",
    )
    # 9,999 sh SGOV.US gives ~$750k Margin AvailableFunds, but wide spread keeps Phase A sell WORKING (0 filled)!
    server.set_position("SGOV.US", 9_999, 100.00, 100.00)
    server.set_quote("SGOV.US", bid=99.60, ask=100.40, close=100.00)
    server.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    _install_fake_ib(monkeypatch, server)

    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            bootstrap_fx_usd=300_000.0,
            allow_fx_convert=True,
            config=ExecutionPolicyConfig(poll_interval_sec=0.0),
        )

    assert rep.phase_a_fills[0].status == "WORKING"
    assert rep.fx_report is None
    assert any("ZERO_MARGIN_FX_GUARD" in a for a in rep.alerts)
    assert all(getattr(r.contract, "secType", "STK") != "CASH" for r in server.orders)
    assert rep.spendable_cash_before_buys == pytest.approx(0.0)
    assert len(rep.phase_b_fills) == 0
    for _ in range(3):
        assert server.settled_cash("USD") >= 0.0
        server.advance_settlement_days(1)


def test_e2e_cash_capped_scaled_buy_preserves_remaining_target_and_fx_crash_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (`L3-C` + `L3-D`): scaled cash-cap buys do not burn full target intent, and FX checkpoints before Phase B."""
    plan = _make_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol="AAPL.US",
                ibkr_symbol="AAPL",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal("0.50000000"),
                sleeve_contributions={"us_sector_quality_mom_v1": "0.50000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            )
        ],
    )
    # Part 1 (`L3-C` + `L3-B` PARTIAL): Run 1 partially sells 400/1,000 SGOV.US -> caps AAPL.US buy < 500 sh;
    # after TWS cancels remaining SGOV.US order and liquidity returns, Run 2 sells remaining 600 SGOV.US
    # and buys the remaining AAPL.US shares up to full ~500 sh target!
    s_cap = FakeTwsServer(usd_cash=100.0, usd_settled_cash=100.0, fill_mode="CROSS_SPREAD")
    s_cap.set_position("SGOV.US", 1_000, 100.00, 100.00)
    s_cap.set_quote("SGOV.US", bid=99.99, ask=100.01, close=100.00)
    s_cap.set_quote("AAPL.US", bid=99.99, ask=100.01, close=100.00)
    s_cap.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    s_cap.set_order_script("SGOV.US", OrderScript(max_cum_fill_by_eval={0: 400}))
    _install_fake_ib(monkeypatch, s_cap)

    state_cap = tmp_path / "state_cap"
    with IbAsyncBrokerAdapter() as adapter1:
        rep1 = execute_market_rebalance(
            plan,
            adapter1,
            market="US",
            dry_run=False,
            state_dir=state_cap,
            config=ExecutionPolicyConfig(poll_interval_sec=0.05),
        )
    assert rep1.phase_a_fills[0].status == "PARTIAL"
    assert rep1.phase_b_fills[0].status == "FILLED"
    first_aapl_qty = rep1.phase_b_fills[0].filled_qty
    assert 390 <= first_aapl_qty < 500

    # Cancel remaining 600 sh SGOV.US order on TWS and restore full liquidity for Run 2
    s_cap.cancel_server_order(s_cap.orders[0].perm_id, status="Cancelled")
    s_cap.scripts.pop("SGOV.US", None)

    with IbAsyncBrokerAdapter() as adapter2:
        rep2 = execute_market_rebalance(
            plan,
            adapter2,
            market="US",
            dry_run=False,
            state_dir=state_cap,
            config=ExecutionPolicyConfig(poll_interval_sec=0.05),
        )
    assert rep2.phase_a_fills[0].status == "FILLED"
    assert rep2.phase_a_fills[0].filled_qty == 600
    assert "SGOV.US" not in s_cap.holdings
    assert len(rep2.phase_b_fills) == 1 and rep2.phase_b_fills[0].status == "FILLED"
    assert s_cap.holdings["AAPL.US"].quantity == 500

    # Part 2 (`L3-D` crash window): Socket drops during Phase B AFTER FX conversion already executed!
    s_fx_crash = FakeTwsServer(
        usd_cash=500_000.0,
        usd_settled_cash=500_000.0,
        usdhkd_bid=7.8000,
        usdhkd_ask=7.8004,
        fill_mode="CROSS_SPREAD",
    )
    s_fx_crash.set_quote("AAPL.US", bid=99.66, ask=100.34, close=100.00)
    s_fx_crash.set_quote("SHY.US", bid=82.92, ask=82.94, close=82.93)
    s_fx_crash.set_order_script("AAPL.US", OrderScript(disconnect_on_poll=True))
    _install_fake_ib(monkeypatch, s_fx_crash)

    state_fx = tmp_path / "state_fx"
    with pytest.raises(ConnectionError, match="Simulated TWS socket drop"), IbAsyncBrokerAdapter(
        IbkrConnectionConfig(client_id=26)
    ) as fx_adapter1:
        execute_market_rebalance(
            plan,
            fx_adapter1,
            market="US",
            dry_run=False,
            bootstrap_fx_usd=100_000.0,
            allow_fx_convert=True,
            state_dir=state_fx,
            config=ExecutionPolicyConfig(poll_interval_sec=1.0),
        )
    assert s_fx_crash.cash_balance["HKD"] == pytest.approx(780_000.0)

    # Reconnect on failover clientId=27: FX checkpointed before Phase B prevents double conversion!
    with IbAsyncBrokerAdapter(IbkrConnectionConfig(client_id=27)) as fx_adapter2:
        fx_rep2 = execute_market_rebalance(
            plan,
            fx_adapter2,
            market="US",
            dry_run=False,
            bootstrap_fx_usd=100_000.0,
            allow_fx_convert=True,
            state_dir=state_fx,
            config=ExecutionPolicyConfig(poll_interval_sec=0.1),
        )
    assert fx_rep2.fx_report is not None and fx_rep2.fx_report.status == "ALREADY_COMPLETED"
    assert s_fx_crash.cash_balance["HKD"] == pytest.approx(780_000.0)


