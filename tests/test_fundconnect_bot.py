"""Comprehensive test suite for standalone `fundconnect-ibkr-bot`."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fundconnect_bot.cli import (
    _find_previous_plan_before,
    _format_plan_markdown,
    _resolve_cache_dirs,
)
from fundconnect_bot.cli import main as cli_main
from fundconnect_bot.config import (
    BOOTSTRAP_SOURCE_SYMBOL,
    HK_DEFENSIVE_SYMBOL,
    US_DEFENSIVE_SYMBOL,
    ExecutionPolicyConfig,
    IbkrConnectionConfig,
    SleeveCadences,
)
from fundconnect_bot.data.calendars import resolve_execution_session, us_open_in_hkt
from fundconnect_bot.execution.broker import (
    CashSnap,
    FxConversionReport,
    IbAsyncBrokerAdapter,
    MockBrokerAdapter,
    OpenOrderSnap,
    OrderSpec,
    PositionSnap,
    Quote,
)
from fundconnect_bot.execution.hkex_rules import (
    floor_to_lot,
    from_ibkr_position_symbol,
    get_hk_board_lot,
    hkex_tick_size,
    round_hkex_price,
    to_ibkr_contract_fields,
)
from fundconnect_bot.execution.policy import (
    ActionCode,
    apply_cash_cap_to_buys,
    build_execution_batch,
    compute_spendable_cash,
    execute_market_rebalance,
    load_execution_state,
)
from fundconnect_bot.strategies.composite import (
    LiveSleeveState,
    PlanTarget,
    WeeklyPlan,
    _should_freeze_sleeve,
    build_weekly_plan,
    load_weekly_plan,
    save_weekly_plan,
)
from fundconnect_bot.strategies.us_macro import compute_us_macro_targets

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_US_CACHE_DIR = Path("/home/obscure/fundconnect-ibkr-bot/data/us")
LOCAL_HK_CACHE_DIR = Path("/home/obscure/fundconnect-ibkr-bot/data/hk")
US_CACHE_DIR = (
    LOCAL_US_CACHE_DIR
    if (LOCAL_US_CACHE_DIR / "indexone").exists() or (LOCAL_US_CACHE_DIR / "us_eod.parquet").exists()
    else Path("/home/obscure/TradeMaster/data/fundconnect_us")
)
HK_CACHE_DIR = (
    LOCAL_HK_CACHE_DIR
    if (LOCAL_HK_CACHE_DIR / "tushare").exists()
    else Path("/home/obscure/TradeMaster/data/fundconnect_hk")
)


@pytest.fixture(scope="module")
def plan_20261113() -> WeeklyPlan:
    return build_weekly_plan(
        asof_date=date(2026, 11, 13),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
    )


def _make_minimal_plan(
    *,
    market: str = "US",
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


# ---------------------------------------------------------------------------
# 1. Zero TradeMaster / External Study Dependency Audit
# ---------------------------------------------------------------------------
def test_zero_trademaster_or_studies_imports() -> None:
    py_files = sorted((REPO_ROOT / "src").rglob("*.py")) + sorted(
        (REPO_ROOT / "tests").rglob("*.py")
    )
    assert len(py_files) >= 15
    forbidden = {"trademaster", "studies", "us_strategies", "hk_strategies"}
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden, f"Forbidden import {alias.name} in {path}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in forbidden, f"Forbidden from-import {node.module} in {path}"


# ---------------------------------------------------------------------------
# 2. Exact 2026-11-13 -> 2026-11-16 Composite Plan Reproduction & SHA-256
# ---------------------------------------------------------------------------
def test_weekly_plan_20261113_exact_composite_weights_and_sha256_roundtrip(
    plan_20261113: WeeklyPlan,
    tmp_path: Path,
) -> None:
    assert plan_20261113.asof_date == date(2026, 11, 13)
    assert plan_20261113.us_execution_session == date(2026, 11, 16)
    assert plan_20261113.hk_execution_session == date(2026, 11, 16)
    assert not plan_20261113.us_deferred_holiday
    assert not plan_20261113.hk_deferred_holiday
    assert plan_20261113.us_open_hkt == "2026-11-16T22:30:00+08:00"

    tmap = plan_20261113.target_map()
    expected_weights = {
        # US Macro (35%) + US SQM (35%) -> 12 US instruments (68.25% total)
        "SHY.US": Decimal("0.43845649"),
        "SCHD.US": Decimal("0.02975465"),
        "ABBV.US": Decimal("0.02808832"),
        "COST.US": Decimal("0.02646893"),
        "DIS.US": Decimal("0.02567321"),
        "V.US": Decimal("0.02316337"),
        "AAPL.US": Decimal("0.02169186"),
        "PG.US": Decimal("0.02153256"),
        "MA.US": Decimal("0.02099889"),
        "MSFT.US": Decimal("0.01634871"),
        "LLY.US": Decimal("0.01573399"),
        "LOW.US": Decimal("0.01458893"),
        # HK ETF (20%) + HK Southbound Blue-Chip (10%) -> 8 HK instruments (23.05% total)
        "3053.HK": Decimal("0.18400000"),
        "0003.HK": Decimal("0.00906838"),
        "0002.HK": Decimal("0.00903376"),
        "0939.HK": Decimal("0.00836512"),
        "0066.HK": Decimal("0.00657989"),
        "3988.HK": Decimal("0.00596006"),
        "0669.HK": Decimal("0.00453398"),
        "9999.HK": Decimal("0.00295879"),
    }
    assert set(tmap) == set(expected_weights)
    for sym, exp_w in expected_weights.items():
        assert tmap[sym].portfolio_weight == exp_w, f"Weight mismatch on {sym}"

    # Check exact HK board lots & IBKR leading-zero stripping in generated plan
    expected_hk_meta = {
        "3053.HK": (1, "3053"),
        "0002.HK": (500, "2"),
        "0003.HK": (1000, "3"),
        "0066.HK": (500, "66"),
        "0669.HK": (100, "669"),
        "0939.HK": (1000, "939"),
        "3988.HK": (1000, "3988"),
        "9999.HK": (100, "9999"),
    }
    for sym, (exp_lot, exp_ibkr) in expected_hk_meta.items():
        assert tmap[sym].lot_size == exp_lot
        assert tmap[sym].ibkr_symbol == exp_ibkr
        assert tmap[sym].exchange == "SEHK"

    saved_path = save_weekly_plan(plan_20261113, tmp_path / "plan_20261113.json")
    reloaded = load_weekly_plan(saved_path)
    assert reloaded.plan_sha256 == plan_20261113.plan_sha256
    assert reloaded.target_map()["0003.HK"].portfolio_weight == Decimal("0.00906838")

    # Tampering with saved JSON must fail cryptographic hash check
    tampered = json.loads(saved_path.read_text(encoding="utf-8"))
    tampered["targets"][0]["portfolio_weight"] = "0.99999999"
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 verification failed"):
        load_weekly_plan(tampered_path)


def test_live_sleeve_state_week_to_week_hysteresis_20261106_to_20261113() -> None:
    plan_prev = build_weekly_plan(
        asof_date=date(2026, 11, 6),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
    )
    live_pw = {t.symbol: float(t.portfolio_weight) for t in plan_prev.targets}
    live_state = LiveSleeveState.from_live_portfolio_weights(live_pw, previous_plan=plan_prev)
    assert live_state.us_macro_prev_regime is not None
    assert live_state.us_macro_weights is not None
    assert "SHY.US" in live_state.us_macro_weights
    assert len(live_state.us_sqm_held_stocks) == 10
    assert live_state.hk_bluechip_weights is not None

    plan_next = build_weekly_plan(
        asof_date=date(2026, 11, 13),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
        live_state=live_state,
    )
    bc_diag = plan_next.sleeve_diagnostics["hk_southbound_bluechip_v1"]
    assert bc_diag["cold_start"] is False
    assert Decimal(plan_next.summary["total_invested_weight"]) <= Decimal("1.00")


# ---------------------------------------------------------------------------
# 3. HKEX Spread Table Rounding, Board Lots & SEHK Leading-Zero Contract Rule
# ---------------------------------------------------------------------------
def test_hkex_tick_spread_table_all_bands_and_board_lots() -> None:
    # Verify every HKEX Part A spread tier across 0.01 .. 9995.00 HKD
    band_checks = [
        (Decimal("0.123"), Decimal("0.001")),
        (Decimal("0.375"), Decimal("0.005")),
        (Decimal("6.28"), Decimal("0.01")),
        (Decimal("7.39"), Decimal("0.01")),
        (Decimal("15.42"), Decimal("0.01")),
        (Decimal("32.56"), Decimal("0.02")),
        (Decimal("81.05"), Decimal("0.05")),
        (Decimal("141.80"), Decimal("0.10")),
        (Decimal("190.40"), Decimal("0.10")),
        (Decimal("320.40"), Decimal("0.20")),
        (Decimal("680.50"), Decimal("0.50")),
        (Decimal("1500.00"), Decimal("1.00")),
        (Decimal("3200.00"), Decimal("2.00")),
        (Decimal("6500.00"), Decimal("5.00")),
    ]
    for px, exp_tick in band_checks:
        assert hkex_tick_size(px) == exp_tick

    # Exact sample prices from actual HK universe + rounding direction checks
    assert round_hkex_price(6.284, "BUY", symbol="3988.HK") == Decimal("6.28")
    assert round_hkex_price(6.284, "SELL", symbol="3988.HK") == Decimal("6.29")
    assert round_hkex_price(7.391, "BUY", symbol="0003.HK") == Decimal("7.39")
    assert round_hkex_price(32.573, "BUY", symbol="0066.HK") == Decimal("32.56")
    assert round_hkex_price(32.573, "SELL", symbol="0066.HK") == Decimal("32.58")
    assert round_hkex_price(81.08, "BUY", symbol="0002.HK") == Decimal("81.05")
    assert round_hkex_price(81.08, "SELL", symbol="0002.HK") == Decimal("81.10")
    assert round_hkex_price(141.87, "BUY", symbol="0669.HK") == Decimal("141.80")
    assert round_hkex_price(190.43, "BUY", symbol="9999.HK") == Decimal("190.40")
    # Band boundary: 200.05 in (200, 500] (step 0.20) must floor BUY to 200.00 and ceil SELL to 200.20
    assert round_hkex_price(Decimal("200.05"), "BUY", symbol="9999.HK") == Decimal("200.00")
    assert round_hkex_price(Decimal("200.05"), "SELL", symbol="9999.HK") == Decimal("200.20")

    # 3053.HK Money Market ETF tick (0.05 default and explicit 0.10 override)
    assert round_hkex_price(1195.63, "BUY", symbol="3053.HK") == Decimal("1195.60")
    assert round_hkex_price(1195.63, "SELL", symbol="3053.HK") == Decimal("1195.65")
    assert round_hkex_price(
        1195.63, "BUY", symbol="3053.HK", money_market_etf_tick=Decimal("0.10")
    ) == Decimal("1195.60")

    # Part D Equity ETF (2828.HK uses 0.02 tick in 50..100 HKD)
    assert hkex_tick_size(Decimal("81.54"), symbol="2828.HK") == Decimal("0.02")
    assert round_hkex_price(81.55, "BUY", symbol="2828.HK") == Decimal("81.54")

    # Board lots & floor_to_lot
    assert get_hk_board_lot("0002.HK") == 500
    assert get_hk_board_lot("0003.HK") == 1000
    assert get_hk_board_lot("0066.HK") == 500
    assert get_hk_board_lot("0669.HK") == 100
    assert get_hk_board_lot("0939.HK") == 1000
    assert get_hk_board_lot("3988.HK") == 1000
    assert get_hk_board_lot("9999.HK") == 100
    assert get_hk_board_lot("3053.HK") == 1
    assert floor_to_lot(2_890, 1000) == 2_000
    assert floor_to_lot(1_499.9, 500) == 1_000

    # IBKR SEHK leading-zero stripping & reverse normalization
    assert to_ibkr_contract_fields("0003.HK") == ("3", "SEHK", "HKD")
    assert to_ibkr_contract_fields("0002.HK") == ("2", "SEHK", "HKD")
    assert to_ibkr_contract_fields("0066.HK") == ("66", "SEHK", "HKD")
    assert to_ibkr_contract_fields("0700.HK") == ("700", "SEHK", "HKD")
    assert to_ibkr_contract_fields("3053.HK") == ("3053", "SEHK", "HKD")
    assert to_ibkr_contract_fields("SHY.US") == ("SHY", "SMART", "USD")
    assert from_ibkr_position_symbol("3", "HKD") == "0003.HK"
    assert from_ibkr_position_symbol("66", "HKD") == "0066.HK"
    assert from_ibkr_position_symbol("3053", "HKD") == "3053.HK"
    assert from_ibkr_position_symbol("SHY", "USD") == "SHY.US"


# ---------------------------------------------------------------------------
# 4–8. 6-Tier Gap / News-Shock Matrix & Wide-Spread / Halt Guards
# ---------------------------------------------------------------------------
def _single_us_stock_plan(symbol: str = "AAPL.US", weight: str = "0.20000000") -> WeeklyPlan:
    return _make_minimal_plan(
        market="US",
        targets=[
            PlanTarget(
                symbol=symbol,
                ibkr_symbol=symbol.removesuffix(".US"),
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="stock",
                portfolio_weight=Decimal(weight),
                sleeve_contributions={"us_sector_quality_mom_v1": weight},
                ref_close=Decimal("200.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="SHY.US",
                ibkr_symbol="SHY",
                market="US",
                exchange="SMART",
                currency="USD",
                asset_class="etf",
                portfolio_weight=Decimal("0.10000000"),
                sleeve_contributions={"us_macro_allweather_v1": "0.10000000"},
                ref_close=Decimal("100.00"),
                lot_size=1,
                defensive_fallback="SHY.US",
                is_defensive_fallback=True,
            ),
        ],
    )


def test_mild_gap_up_dynamically_shrinks_share_count_without_cash_overflow() -> None:
    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    # $100,000 equity -> $20,000 target for AAPL. Friday close=$200 (would be 100 sh),
    # Monday +2.0% mild gap -> mid=$204.00 -> must resize to floor(20000/204) = 98 shares ($19,992 <= $20,000)!
    batch = build_execution_batch(
        plan,
        market="US",
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        open_orders=[],
        quotes={
            "AAPL.US": Quote("AAPL.US", bid=203.95, ask=204.05, last=204.00, prev_close=200.0),
            "SHY.US": Quote("SHY.US", bid=99.99, ask=100.01, last=100.00, prev_close=100.0),
        },
    )
    d_aapl = next(d for d in batch.decisions if d.symbol == "AAPL.US")
    assert d_aapl.action == ActionCode.BUY_NORMAL
    assert d_aapl.desired_total_qty == 98
    assert d_aapl.desired_total_qty * 204.00 <= 20_000.0
    assert batch.redirected_defensive_notional == pytest.approx(0.0)


def test_moderate_bullish_gap_triggers_50_pct_staged_entry_and_defensive_routing() -> None:
    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    # +4.5% gap -> mid=$209.00 -> 50% staged ($10,000 -> floor(10000/209)=47 sh) + $10,000 routed to SHY.US!
    batch = build_execution_batch(
        plan,
        market="US",
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        open_orders=[],
        quotes={
            "AAPL.US": Quote("AAPL.US", bid=208.95, ask=209.05, last=209.00, prev_close=200.0),
            "SHY.US": Quote("SHY.US", bid=99.99, ask=100.01, last=100.00, prev_close=100.0),
        },
        use_adaptive=True,
    )
    d_aapl = next(d for d in batch.decisions if d.symbol == "AAPL.US")
    d_shy = next(d for d in batch.decisions if d.symbol == "SHY.US")
    assert d_aapl.action == ActionCode.BUY_STAGED_50
    assert d_aapl.desired_total_qty == 47
    assert d_aapl.order_style == "ADAPTIVE_PATIENT"
    assert batch.redirected_defensive_notional == pytest.approx(10_000.0)
    assert d_shy.action == ActionCode.BUY_DEFENSIVE_FALLBACK
    # SHY base $10,000 + redirected $10,000 = $20,000 -> 200 shares @ $100
    assert d_shy.desired_total_qty == 200


def test_extreme_bullish_surge_triggers_skip_gap_up_circuit_breaker() -> None:
    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    # +8.0% surge -> mid=$216.00 -> SKIP_GAP_UP + 100% ($20,000) routed to SHY.US ($30,000 total -> 300 sh)
    batch = build_execution_batch(
        plan,
        market="US",
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        open_orders=[],
        quotes={
            "AAPL.US": Quote("AAPL.US", bid=215.95, ask=216.05, last=216.00, prev_close=200.0),
            "SHY.US": Quote("SHY.US", bid=99.99, ask=100.01, last=100.00, prev_close=100.0),
        },
    )
    d_aapl = next(d for d in batch.decisions if d.symbol == "AAPL.US")
    d_shy = next(d for d in batch.decisions if d.symbol == "SHY.US")
    assert d_aapl.action == ActionCode.SKIP_GAP_UP
    assert d_aapl.delta_qty == 0
    assert all(o.symbol != "AAPL.US" for o in batch.phase_b_buys)
    assert batch.redirected_defensive_notional == pytest.approx(20_000.0)
    assert d_shy.desired_total_qty == 300


def test_adverse_bearish_news_gap_triggers_falling_knife_veto() -> None:
    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    # -4.5% weekend bad news gap -> mid=$191.00 -> SKIP_ADVERSE_GAP_DOWN + $20,000 routed to SHY.US
    batch = build_execution_batch(
        plan,
        market="US",
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        open_orders=[],
        quotes={
            "AAPL.US": Quote("AAPL.US", bid=190.95, ask=191.05, last=191.00, prev_close=200.0),
            "SHY.US": Quote("SHY.US", bid=99.99, ask=100.01, last=100.00, prev_close=100.0),
        },
    )
    d_aapl = next(d for d in batch.decisions if d.symbol == "AAPL.US")
    d_shy = next(d for d in batch.decisions if d.symbol == "SHY.US")
    assert d_aapl.action == ActionCode.SKIP_ADVERSE_GAP_DOWN
    assert d_aapl.delta_qty == 0
    assert batch.redirected_defensive_notional == pytest.approx(20_000.0)
    assert d_shy.desired_total_qty == 300


def test_severe_bearish_sell_gap_3_slice_twap_and_flash_crash_floor_guard() -> None:
    # Empty target plan -> held HK stock 0003.HK (6,000 shares, lot=1000) & US stock NVDA.US (200 shares) must exit
    plan = _make_minimal_plan(market="HK", targets=[])
    # Case A: -7.0% severe bearish gap on 0003.HK (ref_close=10.00 -> mid=9.30) -> 3 slices of 2,000 shares
    batch_twap = build_execution_batch(
        plan,
        market="HK",
        positions={"0003.HK": PositionSnap("0003.HK", 6_000, 10.0, 10.0, "HKD")},
        cash={"HKD": CashSnap("HKD", 100_000.0, 100_000.0, 100_000.0)},
        open_orders=[],
        quotes={"0003.HK": Quote("0003.HK", bid=9.29, ask=9.31, last=9.30, prev_close=10.00)},
    )
    d_twap = next(d for d in batch_twap.decisions if d.symbol == "0003.HK")
    assert d_twap.action == ActionCode.SELL_SLICED_TWAP
    assert d_twap.slice_quantities == (2_000, 2_000, 2_000)
    assert len(batch_twap.phase_a_sells) == 3
    assert all(s.quantity % 1000 == 0 for s in batch_twap.phase_a_sells)
    assert all(s.style == "PASSIVE_MID_LIMIT" for s in batch_twap.phase_a_sells)

    # Case B: -15.0% flash-crash / bad-quote gap (ref_close=10.00 -> mid=8.50) -> protective floor at 8.80 (-12%)
    batch_crash = build_execution_batch(
        plan,
        market="HK",
        positions={"0003.HK": PositionSnap("0003.HK", 6_000, 10.0, 10.0, "HKD")},
        cash={"HKD": CashSnap("HKD", 100_000.0, 100_000.0, 100_000.0)},
        open_orders=[],
        quotes={"0003.HK": Quote("0003.HK", bid=8.49, ask=8.51, last=8.50, prev_close=10.00)},
    )
    d_crash = next(d for d in batch_crash.decisions if d.symbol == "0003.HK")
    assert d_crash.action == ActionCode.HOLD_FLASH_CRASH_GUARD
    assert d_crash.order_style == "PROTECTIVE_FLOOR_LIMIT"
    assert d_crash.limit_price == Decimal("8.80")


def test_wide_spread_switches_to_passive_mid_and_halted_quote_skips_safely() -> None:
    plan = _make_minimal_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="9999.HK",
                ibkr_symbol="9999",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.15000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.15000000"},
                ref_close=Decimal("190.40"),
                lot_size=100,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="0669.HK",
                ibkr_symbol="669",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.15000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.15000000"},
                ref_close=Decimal("141.80"),
                lot_size=100,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
        ],
    )
    # 9999.HK has ~90 bps spread (> 60 bps HK limit) -> switches to PASSIVE_MID_LIMIT at mid;
    # 0669.HK has bid=0.0 (halted) -> HALTED_OR_NO_QUOTE and skipped cleanly.
    batch = build_execution_batch(
        plan,
        market="HK",
        positions={},
        cash={"HKD": CashSnap("HKD", 1_000_000.0, 1_000_000.0, 1_000_000.0)},
        open_orders=[],
        quotes={
            "9999.HK": Quote("9999.HK", bid=189.50, ask=191.30, last=190.40, prev_close=190.40),
            "0669.HK": Quote("0669.HK", bid=0.0, ask=141.80, last=0.0, prev_close=141.80),
            "3053.HK": Quote("3053.HK", bid=1195.55, ask=1195.65, last=1195.60, prev_close=1195.60),
        },
    )
    d_9999 = next(d for d in batch.decisions if d.symbol == "9999.HK")
    d_0669 = next(d for d in batch.decisions if d.symbol == "0669.HK")
    assert d_9999.wide_spread is True
    assert d_9999.order_style == "PASSIVE_MID_LIMIT"
    assert d_9999.limit_price == Decimal("190.40")
    assert d_0669.action == ActionCode.HALTED_OR_NO_QUOTE
    assert d_0669.delta_qty == 0
    assert all(o.symbol != "0669.HK" for o in batch.phase_b_buys)


# ---------------------------------------------------------------------------
# 9–11. Sell-Before-Buy, Bounded Walk-Limit, Idempotency & Settled HKD Cap
# ---------------------------------------------------------------------------
def test_sell_before_buy_ordering_and_bounded_walk_limit_repricing(
    plan_20261113: WeeklyPlan,
) -> None:
    # Hold 9,946 SGOV.US ($1,000,000) with $29.16 USD cash; force AAPL.US to require 2 walk-limit reprices
    quotes: dict[str, Quote] = {
        BOOTSTRAP_SOURCE_SYMBOL: Quote(
            BOOTSTRAP_SOURCE_SYMBOL, bid=100.53, ask=100.55, last=100.54, prev_close=100.54
        )
    }
    for t in plan_20261113.targets_for_market("US"):
        rc = float(t.ref_close)
        if t.symbol == "AAPL.US":
            # Spread is 32 bps (< 35 bps US limit): step 0 (+8 bps) -> step 1 (+14 bps) -> step 2 (ask +16 bps)
            quotes[t.symbol] = Quote(
                t.symbol,
                bid=round(rc * 0.9984, 2),
                ask=round(rc * 1.0016, 2),
                last=rc,
                prev_close=rc,
            )
        else:
            quotes[t.symbol] = Quote(
                t.symbol,
                bid=round(rc * 0.9998, 2),
                ask=round(rc * 1.0002, 2),
                last=rc,
                prev_close=rc,
            )

    def custom_fill(spec: OrderSpec, q: Quote, reprice_count: int) -> tuple[int, float]:
        if spec.symbol == "AAPL.US" and reprice_count < 2:
            # Partial fill on initial order, full fill once walked to step 2
            return (spec.quantity // 3 if reprice_count == 1 else 0), q.ask
        return (spec.quantity, q.bid if spec.side == "SELL" else q.ask)

    broker = MockBrokerAdapter(
        positions={
            BOOTSTRAP_SOURCE_SYMBOL: PositionSnap(
                BOOTSTRAP_SOURCE_SYMBOL, 9_946, 100.54, 100.54, "USD"
            )
        },
        cash={"USD": CashSnap("USD", 29.16, 29.16, 29.16)},
        quotes=quotes,
        fill_policy=custom_fill,
    )
    cfg = ExecutionPolicyConfig(poll_interval_sec=0.0)
    rep = execute_market_rebalance(
        plan_20261113,
        broker,
        market="US",
        dry_run=False,
        bootstrap_fx_usd=300_000.0,
        allow_fx_convert=False,
        config=cfg,
    )
    # First submitted order MUST be Phase A SELL of 9,946 SGOV.US before any Phase B BUY
    assert broker.submitted_orders[0].symbol == BOOTSTRAP_SOURCE_SYMBOL
    assert broker.submitted_orders[0].side == "SELL"
    assert broker.submitted_orders[0].quantity == 9_946
    assert broker.submitted_orders[0].phase == "PHASE_A_SELL"
    assert all(o.phase == "PHASE_B_BUY" for o in broker.submitted_orders[1:])

    # Verify AAPL.US walked twice within max_slippage_bps (25 bps) and filled completely
    aapl_fill = next(f for f in rep.phase_b_fills if f.symbol == "AAPL.US")
    assert aapl_fill.status == "FILLED"
    assert aapl_fill.reprice_count == 2
    aapl_spec = next(o for o in broker.submitted_orders if o.symbol == "AAPL.US")
    max_allowed = Decimal(str(aapl_spec.arrival_mid * (1.0 + cfg.max_slippage_bps / 10_000.0)))
    assert aapl_fill.final_limit_price <= max_allowed
    # Verify USD cash stays >= $300,000 reserved for HK sleeve (zero margin borrowing!)
    assert broker.cash["USD"].total_cash >= 300_000.0


def test_idempotency_across_open_orders_and_persisted_state_file(tmp_path: Path) -> None:
    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    quotes = {
        "AAPL.US": Quote("AAPL.US", bid=199.98, ask=200.02, last=200.00, prev_close=200.00),
        "SHY.US": Quote("SHY.US", bid=99.99, ask=100.01, last=100.00, prev_close=100.00),
    }

    # Sub-case A: Open working orders already cover target shares -> 0 new orders
    broker_open = MockBrokerAdapter(
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        quotes=quotes,
        open_orders=[
            OpenOrderSnap("OO-1", "AAPL.US", "BUY", 100, 0, 100, Decimal("200.00"), "Submitted"),
            OpenOrderSnap("OO-2", "SHY.US", "BUY", 100, 0, 100, Decimal("100.00"), "Submitted"),
        ],
    )
    rep_open = execute_market_rebalance(
        plan,
        broker_open,
        market="US",
        dry_run=False,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert len(rep_open.phase_b_fills) == 0
    assert len(broker_open.submitted_orders) == 0

    # Sub-case B: Persisted state file prevents duplicate submission across cron re-runs
    broker_state = MockBrokerAdapter(
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        quotes=quotes,
    )
    rep1 = execute_market_rebalance(
        plan,
        broker_state,
        market="US",
        dry_run=False,
        state_dir=tmp_path,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert len(rep1.phase_b_fills) == 2
    count_after_first = len(broker_state.submitted_orders)

    # Second run against same state_dir (even if positions haven't updated yet) emits 0 orders
    broker_rerun = MockBrokerAdapter(
        positions={},
        cash={"USD": CashSnap("USD", 100_000.0, 100_000.0, 100_000.0)},
        quotes=quotes,
    )
    rep2 = execute_market_rebalance(
        plan,
        broker_rerun,
        market="US",
        dry_run=False,
        state_dir=tmp_path,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert count_after_first == 2
    assert len(rep2.phase_b_fills) == 0
    assert len(broker_rerun.submitted_orders) == 0


def test_zero_margin_interest_hkd_settled_cash_cap_guard(plan_20261113: WeeklyPlan) -> None:
    hk_quotes = {
        t.symbol: Quote(
            t.symbol,
            bid=float(t.ref_close),
            ask=float(t.ref_close),
            last=float(t.ref_close),
            prev_close=float(t.ref_close),
        )
        for t in plan_20261113.targets_for_market("HK")
    }

    # Case 1: Margin account has HK$5,000,000 AvailableFunds (borrowing power) but SettledCash=0 -> 0 buys!
    broker_unsettled = MockBrokerAdapter(
        positions={},
        cash={
            "USD": CashSnap("USD", 700_000.0, 700_000.0, 700_000.0),
            "HKD": CashSnap("HKD", 0.0, 0.0, 5_000_000.0),
        },
        quotes=hk_quotes,
    )
    rep_zero = execute_market_rebalance(
        plan_20261113,
        broker_unsettled,
        market="HK",
        dry_run=False,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert len(rep_zero.capped_phase_b_buys) == 0
    assert len(broker_unsettled.submitted_orders) == 0
    assert any("ZERO_MARGIN_CASH_GUARD" in a for a in rep_zero.alerts)

    # Case 2: Partial settled HKD (HK$600,000 settled < ~HK$1,800,000 desired) -> scales down cleanly
    broker_partial = MockBrokerAdapter(
        positions={},
        cash={
            "USD": CashSnap("USD", 700_000.0, 700_000.0, 700_000.0),
            "HKD": CashSnap("HKD", 600_000.0, 600_000.0, 5_000_000.0),
        },
        quotes=hk_quotes,
    )
    rep_part = execute_market_rebalance(
        plan_20261113,
        broker_partial,
        market="HK",
        dry_run=False,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert len(rep_part.capped_phase_b_buys) > 0
    assert broker_partial.cash["HKD"].total_cash >= 0.0
    assert all(o.quantity % o.lot_size == 0 for o in rep_part.capped_phase_b_buys)


# ---------------------------------------------------------------------------
# 12. US DST Transitions & Exchange Holiday Auto-Deferral
# ---------------------------------------------------------------------------
def test_us_dst_summer_vs_winter_and_monday_holiday_auto_deferral() -> None:
    # Summer EDT (UTC-4): 09:30 ET == 21:30 HKT
    summer_open = us_open_in_hkt(date(2026, 7, 6))
    assert (summer_open.hour, summer_open.minute) == (21, 30)

    # Winter EST (UTC-5): 09:30 ET == 22:30 HKT
    winter_open = us_open_in_hkt(date(2026, 11, 16))
    assert (winter_open.hour, winter_open.minute) == (22, 30)

    # US MLK Jr. Day Monday (2026-01-19) -> auto-defers to Tuesday 2026-01-20
    us_sess, us_def = resolve_execution_session("US", date(2026, 1, 19))
    assert us_sess == date(2026, 1, 20)
    assert us_def is True

    # HK Easter Monday / Ching Ming holiday (2026-04-06) -> auto-defers to first open XHKG session
    hk_sess, hk_def = resolve_execution_session("HK", date(2026, 4, 6))
    assert hk_sess > date(2026, 4, 6)
    assert hk_def is True


# ---------------------------------------------------------------------------
# 13. IbAsyncBrokerAdapter Contract & Adaptive Order Verification
# ---------------------------------------------------------------------------
def test_ib_async_broker_adapter_contract_and_adaptive_order_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mod = types.ModuleType("ib_async")

    @dataclass
    class FakeStock:
        symbol: str
        exchange: str
        currency: str

    @dataclass
    class FakeForex:
        pair: str

    @dataclass
    class FakeTagValue:
        tag: str
        value: str

    @dataclass
    class FakeLimitOrder:
        action: str
        totalQuantity: int
        lmtPrice: float
        tif: str = "DAY"
        orderRef: str = ""
        transmit: bool = True
        account: str = ""
        algoStrategy: str = ""
        algoParams: list[Any] = field(default_factory=list)
        orderId: int = 77

    @dataclass
    class FakeOrderStatus:
        status: str = "Filled"
        filled: float = 1000.0
        remaining: float = 0.0
        avgFillPrice: float = 7.39

    @dataclass
    class FakeTrade:
        contract: Any
        order: Any
        orderStatus: FakeOrderStatus = field(default_factory=FakeOrderStatus)

    @dataclass
    class FakePortfolioItem:
        contract: Any
        position: float
        averageCost: float
        marketPrice: float

    class FakeIB:
        def __init__(self) -> None:
            self.connected = False
            self.placed: list[tuple[Any, Any]] = []

        def connect(self, **_: Any) -> None:
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def qualifyContracts(self, *contracts: Any) -> list[Any]:
            return list(contracts)

        def sleep(self, _: float) -> None:
            return None

        def portfolio(self, *_: Any) -> list[Any]:
            return [
                FakePortfolioItem(FakeStock("3", "SEHK", "HKD"), 2000.0, 7.30, 7.39),
                FakePortfolioItem(FakeStock("SHY", "SMART", "USD"), 500.0, 82.80, 82.93),
            ]

        def placeOrder(self, contract: Any, order: Any) -> Any:
            self.placed.append((contract, order))
            return FakeTrade(
                contract=contract,
                order=order,
                orderStatus=FakeOrderStatus(
                    status="Filled", filled=float(order.totalQuantity), avgFillPrice=order.lmtPrice
                ),
            )

        def reqTickers(self, *_: Any) -> list[Any]:
            return [types.SimpleNamespace(bid=7.7990, ask=7.8010, close=7.8000, last=7.8000)]

    fake_mod.Stock = FakeStock  # type: ignore[attr-defined]
    fake_mod.Forex = FakeForex  # type: ignore[attr-defined]
    fake_mod.TagValue = FakeTagValue  # type: ignore[attr-defined]
    fake_mod.LimitOrder = FakeLimitOrder  # type: ignore[attr-defined]
    fake_mod.IB = FakeIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    with IbAsyncBrokerAdapter(IbkrConnectionConfig(account="DU123456")) as adapter:
        # 1. Verify SEHK leading-zero stripping ("0003.HK" -> Stock(symbol="3", exchange="SEHK", currency="HKD"))
        hk_contract = adapter.build_stock_contract("0003.HK")
        assert hk_contract == FakeStock(symbol="3", exchange="SEHK", currency="HKD")

        # 2. Verify reverse portfolio normalization ("3" + "HKD" -> "0003.HK")
        pos = adapter.get_positions()
        assert "0003.HK" in pos and pos["0003.HK"].quantity == 2000
        assert "SHY.US" in pos and pos["SHY.US"].quantity == 500

        # 3. Verify US Adaptive Patient order construction
        us_spec = OrderSpec(
            intent_id="intent-us-1",
            symbol="AAPL.US",
            ibkr_symbol="AAPL",
            exchange="SMART",
            currency="USD",
            asset_class="stock",
            side="BUY",
            quantity=50,
            lot_size=1,
            limit_price=Decimal("225.10"),
            arrival_mid=225.00,
            style="ADAPTIVE_PATIENT",
            phase="PHASE_B_BUY",
        )
        ib_order = adapter.build_ib_order(us_spec)
        assert ib_order.algoStrategy == "Adaptive"
        assert ib_order.algoParams == [FakeTagValue("adaptivePriority", "Patient")]
        assert ib_order.account == "DU123456"

        rep = adapter.place_order(us_spec, dry_run=False)
        assert rep.status == "FILLED"

        # 4. Verify IDEALPRO FX conversion warning
        fx_rep = adapter.convert_usd_to_hkd(300_000.0, dry_run=True)
        assert fx_rep.pair == "USD.HKD"
        assert "T+1" in fx_rep.settlement_warning and "T+2" in fx_rep.settlement_warning


# ---------------------------------------------------------------------------
# 14. End-to-End CLI Subcommands (`plan`, `simulate-monday`, `execute` guard)
# ---------------------------------------------------------------------------
def test_cli_plan_simulate_monday_and_holiday_enforcement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc_plan = cli_main(
        [
            "plan",
            "--asof",
            "2026-11-13",
            "--from-cache",
            "--us-cache-dir",
            str(US_CACHE_DIR),
            "--hk-cache-dir",
            str(HK_CACHE_DIR),
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc_plan == 0
    plan_file = tmp_path / "plan_20261113.json"
    latest_file = tmp_path / "latest_plan.json"
    assert plan_file.exists() and latest_file.exists()

    rc_sim = cli_main(["simulate-monday", "--plan", str(plan_file)])
    assert rc_sim == 0
    out_sim = capsys.readouterr().out
    assert "周一自动调仓计划书" in out_sim
    assert "SGOV.US" in out_sim
    assert "SKIP_ADVERSE_GAP_DOWN" in out_sim

    # Verify --enforce-session-date defers cleanly without connecting to IBKR when session is in the future
    future_plan = _make_minimal_plan(
        market="HK",
        targets=[],
        hk_session=date(2099, 1, 5),
    )
    future_path = save_weekly_plan(future_plan, tmp_path / "future_plan.json")
    rc_def = cli_main(
        [
            "execute",
            "--plan",
            str(future_path),
            "--market",
            "HK",
            "--enforce-session-date",
        ]
    )
    assert rc_def == 0
    assert "HOLIDAY_DEFERRED" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 15–18. Regression Tests for Subprocess Import Order, IBKR Cash Accounting,
#        ib_async Event-Loop Polling, Worst-Case Cash Cap & Closed-Market Quotes
# ---------------------------------------------------------------------------
def test_clean_subprocess_imports_no_circular_dependency() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    for stmt in (
        "from fundconnect_bot.strategies import build_weekly_plan; assert callable(build_weekly_plan)",
        "from fundconnect_bot.execution import execute_market_rebalance; assert callable(execute_market_rebalance)",
    ):
        proc = subprocess.run(
            [sys.executable, "-c", stmt],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Subprocess import failed for `{stmt}`:\n{proc.stderr}"


def test_ib_async_get_cash_accounting_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class FakeAccountValue:
        tag: str
        value: str
        currency: str

    current_values: list[FakeAccountValue] = []

    class FakeIB:
        def connect(self, **_: Any) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def accountValues(self, *_: Any) -> list[FakeAccountValue]:
            return list(current_values)

    fake_mod = types.ModuleType("ib_async")
    fake_mod.IB = FakeIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    with IbAsyncBrokerAdapter() as adapter:
        # Case 2a & 2b: USD-base account right after T+2 IDEALPRO USD->HKD conversion:
        # TotalCashValue(USD) aggregates USD+HKD ($1,000,000), while CashBalance(USD)=$700,000
        # and SettledCash(HKD)=0.0 despite CashBalance(HKD)=2,340,000.
        current_values = [
            FakeAccountValue("TotalCashValue", "1000000.00", "USD"),
            FakeAccountValue("CashBalance", "700000.00", "USD"),
            FakeAccountValue("SettledCash", "700000.00", "USD"),
            FakeAccountValue("AvailableFunds", "1000000.00", "USD"),
            FakeAccountValue("CashBalance", "2340000.00", "HKD"),
            FakeAccountValue("TotalCashBalance", "2340000.00", "HKD"),
            FakeAccountValue("SettledCash", "0.00", "HKD"),
        ]
        cash_a = adapter.get_cash()
        assert cash_a["USD"].total_cash == pytest.approx(700_000.0)
        assert cash_a["HKD"].total_cash == pytest.approx(2_340_000.0)
        assert cash_a["HKD"].settled_cash == pytest.approx(0.0)
        assert compute_spendable_cash(cash_a["HKD"]) == pytest.approx(0.0)

        # Case 2c: HKD-base account after Monday Phase A sells SGOV.US:
        # IBKR emits AvailableFunds only in HKD (no AvailableFunds row for USD),
        # CashBalance(USD)=1,000,000 (T+1 trade-dated), SettledCash(USD)=29.16.
        current_values = [
            FakeAccountValue("CashBalance", "1000000.00", "USD"),
            FakeAccountValue("SettledCash", "29.16", "USD"),
            FakeAccountValue("CashBalance", "0.00", "HKD"),
            FakeAccountValue("SettledCash", "0.00", "HKD"),
            FakeAccountValue("AvailableFunds", "7800000.00", "HKD"),
        ]
        cash_c = adapter.get_cash()
        assert cash_c["USD"].total_cash == pytest.approx(1_000_000.0)
        assert cash_c["USD"].settled_cash == pytest.approx(29.16)
        assert cash_c["USD"].available_funds == pytest.approx(1_000_000.0)
        assert compute_spendable_cash(cash_c["USD"]) == pytest.approx(1_000_000.0)
        assert compute_spendable_cash(cash_c["HKD"]) == pytest.approx(0.0)

        # Case 2d: Real IB Gateway 10.3x+ `$LEDGER-CashBalance`, `$LEDGER-TotalCashBalance`,
        # and `SettledCashByDate` (while ignoring `currency='BASE'`):
        current_values = [
            FakeAccountValue("$LEDGER-CashBalance", "9999999.00", "BASE"),
            FakeAccountValue("$LEDGER-CashBalance", "321799.75", "USD"),
            FakeAccountValue("$LEDGER-TotalCashBalance", "321799.75", "USD"),
            FakeAccountValue(
                "SettledCashByDate",
                "20261116:-3537.68;20261117:321799.75",
                "USD",
            ),
            FakeAccountValue("$LEDGER-TotalCashBalance", "150000.00", "HKD"),
            FakeAccountValue(
                "$LEDGER-SettledCashByDate",
                "20261116:0.00;20261118:150000.00",
                "HKD",
            ),
        ]
        cash_d = adapter.get_cash()
        assert cash_d["USD"].total_cash == pytest.approx(321_799.75)
        assert cash_d["USD"].settled_cash == pytest.approx(-3_537.68)
        assert cash_d["USD"].available_funds == pytest.approx(321_799.75)
        assert cash_d["HKD"].total_cash == pytest.approx(150_000.0)
        assert cash_d["HKD"].settled_cash == pytest.approx(0.0)
        assert compute_spendable_cash(cash_d["HKD"]) == pytest.approx(0.0)

        # Case 2e: Bare `CashBalance` / `SettledCash` win over `$LEDGER-*` / `SettledCashByDate`
        # regardless of tag arrival order:
        current_values = [
            FakeAccountValue("$LEDGER-CashBalance", "999.00", "USD"),
            FakeAccountValue("CashBalance", "500.00", "USD"),
            FakeAccountValue("SettledCashByDate", "20261116:100.00;20261117:500.00", "USD"),
            FakeAccountValue("SettledCash", "250.00", "USD"),
        ]
        cash_e = adapter.get_cash()
        assert cash_e["USD"].total_cash == pytest.approx(500.0)
        assert cash_e["USD"].settled_cash == pytest.approx(250.0)


def test_ib_async_convert_usd_to_hkd_fxconv_fallback_to_idealpro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class FakeForex:
        pair: str
        exchange: str = "IDEALPRO"
        secType: str = "CASH"
        conId: int = 0

    @dataclass
    class FakeLimitOrder:
        action: str
        totalQuantity: int
        lmtPrice: float
        tif: str = "DAY"
        transmit: bool = True
        account: str = ""
        orderId: int = 901

    class FakeIB:
        reject_fxconv: bool = False
        return_none_on_reject: bool = False
        placed: list[tuple[FakeForex, FakeLimitOrder]]

        def __init__(self) -> None:
            self.placed = []

        def connect(self, **_: Any) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def sleep(self, _: float) -> None:
            return None

        def qualifyContracts(self, *contracts: Any) -> list[Any]:
            out: list[Any] = []
            for c in contracts:
                if getattr(c, "exchange", "") == "FXCONV" and self.reject_fxconv:
                    c.conId = 0
                    if self.return_none_on_reject:
                        out.append(None)
                    continue
                c.conId = 12345777 if getattr(c, "exchange", "") == "IDEALPRO" else 88888
                out.append(c)
            return out

        def reqTickers(self, *_: Any) -> list[Any]:
            return [types.SimpleNamespace(bid=7.7995, ask=7.8005, close=7.8000, last=7.8000)]

        def placeOrder(self, contract: Any, order: Any) -> Any:
            self.placed.append((contract, order))
            return types.SimpleNamespace(
                contract=contract,
                order=order,
                orderStatus=types.SimpleNamespace(status="Submitted", filled=0.0),
            )

    fake_mod = types.ModuleType("ib_async")
    fake_mod.Forex = FakeForex  # type: ignore[attr-defined]
    fake_mod.LimitOrder = FakeLimitOrder  # type: ignore[attr-defined]
    fake_mod.IB = FakeIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    # 1. FXCONV qualifies cleanly -> keeps FXCONV
    FakeIB.reject_fxconv = False
    FakeIB.return_none_on_reject = False
    with IbAsyncBrokerAdapter() as adapter_fxconv:
        rep_fxconv = adapter_fxconv.convert_usd_to_hkd(100_000.0, dry_run=False)
        assert rep_fxconv.status == "Submitted"
        placed_contract, _ = adapter_fxconv._ib.placed[0]
        assert placed_contract.exchange == "FXCONV"
        assert placed_contract.conId == 88888

    # 2. FXCONV returns [] + conId=0 (Error 200 on USD-base IB Gateway 10.3x+) -> IDEALPRO fallback
    FakeIB.reject_fxconv = True
    FakeIB.return_none_on_reject = False
    with IbAsyncBrokerAdapter() as adapter_idealpro:
        rep_idealpro = adapter_idealpro.convert_usd_to_hkd(300_000.0, dry_run=False)
        assert rep_idealpro.status == "Submitted"
        assert rep_idealpro.sell_amount == 300_000
        placed_contract, placed_order = adapter_idealpro._ib.placed[0]
        assert placed_contract.exchange == "IDEALPRO"
        assert placed_contract.conId == 12345777
        assert placed_order.action == "SELL"
        assert placed_order.totalQuantity == 300_000

    # 3. FXCONV returns [None] + conId=0 (real ib_async on Error 200) -> IDEALPRO fallback
    FakeIB.reject_fxconv = True
    FakeIB.return_none_on_reject = True
    with IbAsyncBrokerAdapter() as adapter_none:
        rep_none = adapter_none.convert_usd_to_hkd(300_000.0, dry_run=False)
        assert rep_none.status == "Submitted"
        assert rep_none.sell_amount == 300_000
        placed_contract, placed_order = adapter_none._ib.placed[0]
        assert placed_contract.exchange == "IDEALPRO"
        assert placed_contract.conId == 12345777
        assert placed_order.action == "SELL"
        assert placed_order.totalQuantity == 300_000


def test_fx_validation_error_not_persisted_in_completed_intents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    quotes = {
        "AAPL.US": Quote("AAPL.US", bid=199.98, ask=200.02, last=200.00, prev_close=200.00),
        "SHY.US": Quote("SHY.US", bid=99.99, ask=100.01, last=100.00, prev_close=100.00),
    }
    state_dir = tmp_path / "state"
    broker = MockBrokerAdapter(
        positions={},
        cash={"USD": CashSnap("USD", 500_000.0, 500_000.0, 500_000.0)},
        quotes=quotes,
    )

    def fail_validation_fx(
        usd_amount: float, *, limit_rate: float | None = None, dry_run: bool = True
    ) -> FxConversionReport:
        _ = (limit_rate, dry_run)
        return FxConversionReport(
            pair="USD.HKD",
            sell_currency="USD",
            buy_currency="HKD",
            sell_amount=round(usd_amount),
            estimated_buy_amount=round(usd_amount * 7.8, 2),
            rate=7.8,
            order_id="101",
            status="ValidationError",
            settlement_warning="Warning 321: invalid FXCONV contract",
        )

    monkeypatch.setattr(broker, "convert_usd_to_hkd", fail_validation_fx)
    rep_fail = execute_market_rebalance(
        plan,
        broker,
        market="US",
        dry_run=False,
        bootstrap_fx_usd=450_000.0,
        allow_fx_convert=True,
        state_dir=state_dir,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert rep_fail.fx_report is not None and rep_fail.fx_report.status == "ValidationError"
    # Reserve $450k USD must remain unspent for HK (spendable <= $50k -> Phase B buys scaled down)
    assert broker.cash["USD"].total_cash >= 450_000.0

    state_file = (
        state_dir / f"execution_state_{plan.us_execution_session.strftime('%Y%m%d')}_US.json"
    )
    saved = load_execution_state(state_file, expected_plan_sha256=plan.plan_sha256)
    assert saved is not None
    assert not any(
        entry.get("symbol") == "USD.HKD" for entry in saved.completed_intents.values()
    )

    # Subsequent retry after FX fix MUST attempt FX conversion (not report ALREADY_COMPLETED)
    broker_retry = MockBrokerAdapter(
        positions={},
        cash={"USD": CashSnap("USD", 500_000.0, 500_000.0, 500_000.0)},
        quotes=quotes,
    )
    rep_retry = execute_market_rebalance(
        plan,
        broker_retry,
        market="US",
        dry_run=False,
        bootstrap_fx_usd=450_000.0,
        allow_fx_convert=True,
        state_dir=state_dir,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert rep_retry.fx_report is not None and rep_retry.fx_report.status == "FILLED"
    saved_after = load_execution_state(state_file, expected_plan_sha256=plan.plan_sha256)
    assert saved_after is not None
    assert any(
        entry.get("symbol") == "USD.HKD" and entry.get("status") == "FILLED"
        for entry in saved_after.completed_intents.values()
    )


def test_ib_async_walk_limit_pumps_ib_sleep_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []

    @dataclass
    class FakeStock:
        symbol: str
        exchange: str
        currency: str

    @dataclass
    class FakeLimitOrder:
        action: str
        totalQuantity: int
        lmtPrice: float
        tif: str = "DAY"
        orderRef: str = ""
        transmit: bool = True
        account: str = ""
        algoStrategy: str = ""
        algoParams: list[Any] = field(default_factory=list)
        orderId: int = 88

    @dataclass
    class FakeOrderStatus:
        status: str = "Submitted"
        filled: float = 0.0
        remaining: float = 100.0
        avgFillPrice: float = 0.0

    @dataclass
    class FakeTrade:
        contract: Any
        order: Any
        orderStatus: FakeOrderStatus

    class FakeIB:
        def __init__(self) -> None:
            self.active_trade: FakeTrade | None = None

        def connect(self, **_: Any) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def qualifyContracts(self, *contracts: Any) -> list[Any]:
            return list(contracts)

        def sleep(self, sec: float) -> None:
            sleep_calls.append(sec)
            # Simulate TWS fill arriving while ib.sleep(poll_interval_sec) pumps socket events!
            if sec >= 1.5 and self.active_trade is not None:
                self.active_trade.orderStatus.status = "Filled"
                self.active_trade.orderStatus.filled = float(self.active_trade.order.totalQuantity)
                self.active_trade.orderStatus.remaining = 0.0
                self.active_trade.orderStatus.avgFillPrice = float(self.active_trade.order.lmtPrice)

        def portfolio(self, *_: Any) -> list[Any]:
            return []

        def accountValues(self, *_: Any) -> list[Any]:
            return [
                types.SimpleNamespace(tag="CashBalance", value="50000.0", currency="USD"),
                types.SimpleNamespace(tag="SettledCash", value="50000.0", currency="USD"),
                types.SimpleNamespace(tag="AvailableFunds", value="50000.0", currency="USD"),
            ]

        def reqOpenOrders(self) -> None:
            return None

        def openTrades(self) -> list[Any]:
            return []

        def reqTickers(self, *contracts: Any) -> list[Any]:
            out = []
            for c in contracts:
                if getattr(c, "pair", "") == "USDHKD":
                    out.append(types.SimpleNamespace(bid=7.80, ask=7.80, close=7.80, last=7.80))
                else:
                    out.append(
                        types.SimpleNamespace(bid=199.80, ask=200.20, close=200.00, last=200.00)
                    )
            return out

        def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
            tr = FakeTrade(
                contract=contract,
                order=order,
                orderStatus=FakeOrderStatus(
                    status="Submitted", filled=0.0, remaining=float(order.totalQuantity)
                ),
            )
            self.active_trade = tr
            return tr

    fake_mod = types.ModuleType("ib_async")
    fake_mod.Stock = FakeStock  # type: ignore[attr-defined]
    fake_mod.Forex = lambda pair: types.SimpleNamespace(pair=pair)  # type: ignore[attr-defined]
    fake_mod.TagValue = lambda t, v: (t, v)  # type: ignore[attr-defined]
    fake_mod.LimitOrder = FakeLimitOrder  # type: ignore[attr-defined]
    fake_mod.IB = FakeIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    plan = _single_us_stock_plan("AAPL.US", "0.20000000")
    cfg = ExecutionPolicyConfig(poll_interval_sec=1.75)
    with IbAsyncBrokerAdapter() as adapter:
        rep = execute_market_rebalance(
            plan,
            adapter,
            market="US",
            dry_run=False,
            config=cfg,
        )
    assert any(abs(s - 1.75) < 1e-9 for s in sleep_calls)
    assert all(f.status == "FILLED" for f in rep.phase_b_fills)


def test_worst_case_walk_cash_cap_closed_market_quote_filter_and_missing_shy_bar() -> None:
    # 4a: Worst-case walk-limit (+25 bps) + fee buffer reservation prevents negative cash on walk
    spec = OrderSpec(
        intent_id="cap-1",
        symbol="AAPL.US",
        ibkr_symbol="AAPL",
        exchange="SMART",
        currency="USD",
        asset_class="stock",
        side="BUY",
        quantity=100,
        lot_size=1,
        limit_price=Decimal("100.08"),
        arrival_mid=100.00,
        style="ADAPTIVE_NORMAL",
        phase="PHASE_B_BUY",
    )
    cfg = ExecutionPolicyConfig()
    # $10,030 < 100 * 100.25 * 1.0015 ($10,040.04), so 100 shares must be scaled down to 99 shares!
    capped, warnings = apply_cash_cap_to_buys([spec], spendable_cash=10_030.0, config=cfg)
    assert len(capped) == 1
    assert capped[0].quantity == 99
    assert len(warnings) == 1

    # 4b: Executing HK market must NOT request quotes for closed US holdings (SGOV.US / AAPL.US)
    requested_quote_batches: list[list[str]] = []

    class TrackingMockBroker(MockBrokerAdapter):
        def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
            requested_quote_batches.append(list(symbols))
            return super().get_quotes(symbols)

    hk_plan = _make_minimal_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="0003.HK",
                ibkr_symbol="3",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.10000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.10000000"},
                ref_close=Decimal("7.39"),
                lot_size=1000,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            )
        ],
    )
    broker = TrackingMockBroker(
        positions={
            "AAPL.US": PositionSnap("AAPL.US", 100, 200.0, 220.0, "USD"),
            "0003.HK": PositionSnap("0003.HK", 1000, 7.30, 7.39, "HKD"),
        },
        cash={
            "USD": CashSnap("USD", 50_000.0, 50_000.0, 50_000.0),
            "HKD": CashSnap("HKD", 200_000.0, 200_000.0, 200_000.0),
        },
        quotes={
            "0003.HK": Quote("0003.HK", bid=7.38, ask=7.40, last=7.39, prev_close=7.39),
            "3053.HK": Quote("3053.HK", bid=1195.55, ask=1195.65, last=1195.60, prev_close=1195.60),
        },
    )
    hk_rep = execute_market_rebalance(
        hk_plan,
        broker,
        market="HK",
        dry_run=False,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert all(not sym.endswith(".US") for batch in requested_quote_batches for sym in batch)
    # Closed AAPL.US position ($22,000) + $50,000 USD cash + HKD assets still valued accurately
    assert hk_rep.batch.total_equity_usd > 72_000.0

    # 4c: Missing SHY.US bar on asof_date raises explicit ValueError instead of silent empty dict
    with pytest.raises(ValueError, match="SHY.US lacks EOD bar"):
        compute_us_macro_targets(date(2026, 11, 13), {}, [])


# ---------------------------------------------------------------------------
# 19–20. Week-2+ HK Rotation Same-Session T+2 Funding & CLI Plan Re-Run Idempotency
# ---------------------------------------------------------------------------
def test_week2_hk_rotation_phase_a_sehk_t2_sell_funds_phase_b_buys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rot_plan = _make_minimal_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="0939.HK",
                ibkr_symbol="939",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.15000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.15000000"},
                ref_close=Decimal("10.50"),
                lot_size=1000,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
            PlanTarget(
                symbol="3988.HK",
                ibkr_symbol="3988",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.15000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.15000000"},
                ref_close=Decimal("6.28"),
                lot_size=1000,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            ),
        ],
    )
    hk_quotes = {
        "3053.HK": Quote("3053.HK", bid=1195.55, ask=1195.65, last=1195.60, prev_close=1195.60),
        "0939.HK": Quote("0939.HK", bid=10.49, ask=10.51, last=10.50, prev_close=10.50),
        "3988.HK": Quote("3988.HK", bid=6.27, ask=6.29, last=6.28, prev_close=6.28),
    }

    # Sub-case 1: Dry-run mode with 1,950 sh 3053.HK (~HK$2.33M) + HK$150 settled cash
    mock_dry = MockBrokerAdapter(
        positions={"3053.HK": PositionSnap("3053.HK", 1950, 1190.0, 1195.60, "HKD")},
        cash={
            "USD": CashSnap("USD", 700_000.0, 700_000.0, 700_000.0),
            "HKD": CashSnap("HKD", 150.0, 150.0, 150.0),
        },
        quotes=hk_quotes,
    )
    rep_dry = execute_market_rebalance(
        rot_plan,
        mock_dry,
        market="HK",
        dry_run=True,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert rep_dry.spendable_cash_before_buys > 2_300_000.0
    assert {o.symbol for o in rep_dry.capped_phase_b_buys} == {"0939.HK", "3988.HK"}

    # Sub-case 2: Live IbAsyncBrokerAdapter on a USD-base IBKR account (AvailableFunds emitted
    # ONLY for USD; SettledCash(HKD) stays 150.0 even after 3053.HK sells on SEHK T+2!)
    @dataclass
    class FakeStock:
        symbol: str
        exchange: str
        currency: str

    @dataclass
    class FakeLimitOrder:
        action: str
        totalQuantity: int
        lmtPrice: float
        tif: str = "DAY"
        orderRef: str = ""
        transmit: bool = True
        account: str = ""
        algoStrategy: str = ""
        algoParams: list[Any] = field(default_factory=list)
        orderId: int = 1

    class FakeUsdBaseIB:
        def __init__(self) -> None:
            self._oid = 500
            self.placed: list[tuple[Any, Any]] = []

        def connect(self, **_: Any) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def sleep(self, _: float) -> None:
            return None

        def qualifyContracts(self, *contracts: Any) -> list[Any]:
            return list(contracts)

        def portfolio(self, *_: Any) -> list[Any]:
            return [
                types.SimpleNamespace(
                    contract=FakeStock("3053", "SEHK", "HKD"),
                    position=1950.0,
                    averageCost=1190.0,
                    marketPrice=1195.60,
                ),
                types.SimpleNamespace(
                    contract=FakeStock("SHY", "SMART", "USD"),
                    position=8000.0,
                    averageCost=81.0,
                    marketPrice=81.17,
                ),
            ]

        def accountValues(self, *_: Any) -> list[Any]:
            # USD-base account: AvailableFunds emitted ONLY in USD; SettledCash(HKD) stays 150.0!
            return [
                types.SimpleNamespace(tag="CashBalance", value="50000.00", currency="USD"),
                types.SimpleNamespace(tag="SettledCash", value="50000.00", currency="USD"),
                types.SimpleNamespace(tag="AvailableFunds", value="950000.00", currency="USD"),
                types.SimpleNamespace(tag="CashBalance", value="150.00", currency="HKD"),
                types.SimpleNamespace(tag="SettledCash", value="150.00", currency="HKD"),
            ]

        def reqOpenOrders(self) -> None:
            return None

        def openTrades(self) -> list[Any]:
            return []

        def reqTickers(self, *contracts: Any) -> list[Any]:
            out = []
            for c in contracts:
                if getattr(c, "pair", "") == "USDHKD":
                    out.append(types.SimpleNamespace(bid=7.80, ask=7.80, close=7.80, last=7.80))
                elif c.symbol == "3053":
                    out.append(
                        types.SimpleNamespace(
                            bid=1195.55, ask=1195.65, close=1195.60, last=1195.60
                        )
                    )
                elif c.symbol == "939":
                    out.append(types.SimpleNamespace(bid=10.49, ask=10.51, close=10.50, last=10.50))
                else:
                    out.append(types.SimpleNamespace(bid=6.27, ask=6.29, close=6.28, last=6.28))
            return out

        def placeOrder(self, contract: Any, order: Any) -> Any:
            self._oid += 1
            order.orderId = self._oid
            self.placed.append((contract, order))
            return types.SimpleNamespace(
                contract=contract,
                order=order,
                orderStatus=types.SimpleNamespace(
                    status="Filled",
                    filled=float(order.totalQuantity),
                    remaining=0.0,
                    avgFillPrice=float(order.lmtPrice),
                ),
            )

    fake_mod = types.ModuleType("ib_async")
    fake_mod.Stock = FakeStock  # type: ignore[attr-defined]
    fake_mod.Forex = lambda pair: types.SimpleNamespace(pair=pair)  # type: ignore[attr-defined]
    fake_mod.TagValue = lambda t, v: (t, v)  # type: ignore[attr-defined]
    fake_mod.LimitOrder = FakeLimitOrder  # type: ignore[attr-defined]
    fake_mod.IB = FakeUsdBaseIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    with IbAsyncBrokerAdapter() as live_adapter:
        rep_live = execute_market_rebalance(
            rot_plan,
            live_adapter,
            market="HK",
            dry_run=False,
            config=ExecutionPolicyConfig(poll_interval_sec=0.0),
        )
    assert len(rep_live.phase_a_fills) == 1 and rep_live.phase_a_fills[0].symbol == "3053.HK"
    assert rep_live.spendable_cash_before_buys > 2_300_000.0
    assert {f.symbol for f in rep_live.phase_b_fills} == {"0939.HK", "3988.HK"}
    assert all(f.status == "FILLED" for f in rep_live.phase_b_fills)

    # Sub-case 3: Combined account with HK$1,000,000 unsettled FX + 500 sh 3053.HK (~HK$597,800):
    # Phase B must spend ONLY (150 settled + ~597,000 SEHK sale) and leave HK$1,000,000 unsettled FX untouched!
    mock_combined = MockBrokerAdapter(
        positions={"3053.HK": PositionSnap("3053.HK", 500, 1190.0, 1195.60, "HKD")},
        cash={
            "USD": CashSnap("USD", 700_000.0, 700_000.0, 700_000.0),
            "HKD": CashSnap("HKD", 1_000_150.0, 150.0, 1_000_150.0),
        },
        quotes=hk_quotes,
    )
    rep_comb = execute_market_rebalance(
        rot_plan,
        mock_combined,
        market="HK",
        dry_run=False,
        config=ExecutionPolicyConfig(poll_interval_sec=0.0),
    )
    assert 590_000.0 < rep_comb.spendable_cash_before_buys < 600_000.0
    assert len(rep_comb.phase_b_fills) > 0
    assert mock_combined.cash["HKD"].total_cash >= 1_000_000.0


def test_cli_plan_rerun_selects_strictly_older_previous_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _resolve_cache_dirs(REPO_ROOT / "data", None, None) == (US_CACHE_DIR, HK_CACHE_DIR)
    rc_prev = cli_main(
        [
            "plan",
            "--asof",
            "2026-11-06",
            "--from-cache",
            "--data-dir",
            str(REPO_ROOT / "data"),
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc_prev == 0
    plan_06 = load_weekly_plan(tmp_path / "plan_20261106.json")

    class FakeHoldingsIB:
        def connect(self, **_: Any) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def qualifyContracts(self, *contracts: Any) -> list[Any]:
            return list(contracts)

        def portfolio(self, *_: Any) -> list[Any]:
            return [
                types.SimpleNamespace(
                    contract=types.SimpleNamespace(symbol="3", currency="HKD"),
                    position=10_000.0,
                    averageCost=7.30,
                    marketPrice=7.39,
                ),
                types.SimpleNamespace(
                    contract=types.SimpleNamespace(symbol="SHY", currency="USD"),
                    position=5_000.0,
                    averageCost=81.0,
                    marketPrice=81.17,
                ),
            ]

        def accountValues(self, *_: Any) -> list[Any]:
            return [
                types.SimpleNamespace(tag="CashBalance", value="500000.0", currency="USD"),
                types.SimpleNamespace(tag="SettledCash", value="500000.0", currency="USD"),
                types.SimpleNamespace(tag="CashBalance", value="100000.0", currency="HKD"),
                types.SimpleNamespace(tag="SettledCash", value="100000.0", currency="HKD"),
            ]

        def reqTickers(self, *contracts: Any) -> list[Any]:
            out = []
            for c in contracts:
                if getattr(c, "pair", "") == "USDHKD":
                    out.append(types.SimpleNamespace(bid=7.80, ask=7.80, close=7.80, last=7.80))
                elif getattr(c, "symbol", "") == "3":
                    out.append(types.SimpleNamespace(bid=7.38, ask=7.40, close=7.39, last=7.39))
                else:
                    out.append(types.SimpleNamespace(bid=81.16, ask=81.18, close=81.17, last=81.17))
            return out

    fake_mod = types.ModuleType("ib_async")
    fake_mod.Stock = lambda symbol, exchange, currency: types.SimpleNamespace(  # type: ignore[attr-defined]
        symbol=symbol, exchange=exchange, currency=currency
    )
    fake_mod.Forex = lambda pair: types.SimpleNamespace(pair=pair)  # type: ignore[attr-defined]
    fake_mod.IB = FakeHoldingsIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    args_13 = [
        "plan",
        "--asof",
        "2026-11-13",
        "--from-cache",
        "--sync-ibkr-holdings",
        "--us-cache-dir",
        str(US_CACHE_DIR),
        "--hk-cache-dir",
        str(HK_CACHE_DIR),
        "--out-dir",
        str(tmp_path),
    ]
    assert cli_main(args_13) == 0
    first_13 = load_weekly_plan(tmp_path / "plan_20261113.json")

    # Even after plan_20261113.json exists in tmp_path, _find_previous_plan_before(2026-11-13)
    # MUST return plan_20261106 (strictly older), so re-running `plan` is 100% target-idempotent!
    resolved_prev = _find_previous_plan_before(tmp_path, date(2026, 11, 13))
    assert resolved_prev is not None and resolved_prev.asof_date == plan_06.asof_date

    assert cli_main(args_13) == 0
    second_13 = load_weekly_plan(tmp_path / "plan_20261113.json")
    assert [t.to_dict() for t in second_13.targets] == [t.to_dict() for t in first_13.targets]
    assert second_13.sleeve_diagnostics == first_13.sleeve_diagnostics


# ---------------------------------------------------------------------------
# 21. Hybrid C Rebalancing Cadence (2W AW + 1W SQM + 1W HK_ETF + 2W HK_BC)
# ---------------------------------------------------------------------------
def _sleeve_contrib_map(plan: WeeklyPlan, sleeve_name: str) -> dict[str, Decimal]:
    return {
        t.symbol: Decimal(t.sleeve_contributions[sleeve_name])
        for t in plan.targets
        if sleeve_name in t.sleeve_contributions
    }


def test_sleeve_cadences_config_and_freeze_guards() -> None:
    hc = SleeveCadences.hybrid_c()
    assert (hc.us_macro_weeks, hc.us_sqm_weeks, hc.hk_etf_weeks, hc.hk_bluechip_weeks) == (
        2,
        1,
        1,
        2,
    )
    assert SleeveCadences() == hc

    u1 = SleeveCadences.uniform_1w()
    assert (u1.us_macro_weeks, u1.us_sqm_weeks, u1.hk_etf_weeks, u1.hk_bluechip_weeks) == (
        1,
        1,
        1,
        1,
    )

    with pytest.raises(ValueError, match=">= 1"):
        SleeveCadences(us_macro_weeks=0)
    with pytest.raises(ValueError, match=">= 1"):
        SleeveCadences(hk_bluechip_weeks=-1)

    prev = _make_minimal_plan(
        market="HK",
        targets=[
            PlanTarget(
                symbol="0939.HK",
                ibkr_symbol="939",
                market="HK",
                exchange="SEHK",
                currency="HKD",
                asset_class="stock",
                portfolio_weight=Decimal("0.05000000"),
                sleeve_contributions={"hk_southbound_bluechip_v1": "0.05000000"},
                ref_close=Decimal("10.50"),
                lot_size=1000,
                defensive_fallback="3053.HK",
                is_defensive_fallback=False,
            )
        ],
    )
    # 7 days after 2026-11-13 -> within 11-day freeze window when 0939.HK is active
    freeze_ok, last_eval, fw = _should_freeze_sleeve(
        "hk_southbound_bluechip_v1",
        date(2026, 11, 20),
        2,
        prev,
        {"0939.HK", "3053.HK"},
        Decimal("0.10"),
    )
    assert freeze_ok is True
    assert last_eval == date(2026, 11, 13)
    assert fw == {"0939.HK": Decimal("0.50000000")}

    # Missing/suspended symbol on target_asof cleanly falls back to re-evaluation
    freeze_missing, _, _ = _should_freeze_sleeve(
        "hk_southbound_bluechip_v1",
        date(2026, 11, 20),
        2,
        prev,
        {"3053.HK"},
        Decimal("0.10"),
    )
    assert freeze_missing is False

    # Shortened Thanksgiving week (Wednesday 11d after 2026-11-13 -> 2026-11-24) evaluates fresh
    freeze_11d, _, _ = _should_freeze_sleeve(
        "hk_southbound_bluechip_v1",
        date(2026, 11, 24),
        2,
        prev,
        {"0939.HK", "3053.HK"},
        Decimal("0.10"),
    )
    assert freeze_11d is False

    # Non-positive elapsed days does not freeze
    freeze_same_day, _, _ = _should_freeze_sleeve(
        "hk_southbound_bluechip_v1",
        date(2026, 11, 13),
        2,
        prev,
        {"0939.HK"},
        Decimal("0.10"),
    )
    assert freeze_same_day is False


def test_hybrid_c_three_week_sequential_progression_20261030_to_20261113() -> None:
    # Week 0 (2026-10-30): cold start -> all 4 sleeves EVALUATED
    w0 = build_weekly_plan(
        asof_date=date(2026, 10, 30),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
        cadences=SleeveCadences.hybrid_c(),
    )
    assert w0.summary["cadence_profile"] == "hybrid_c"
    for sleeve in (
        "us_macro_allweather_v1",
        "us_sector_quality_mom_v1",
        "hk_etf_southbound_rot_v1",
        "hk_southbound_bluechip_v1",
    ):
        assert w0.sleeve_diagnostics[sleeve]["cadence_action"] == "EVALUATED"
        assert w0.sleeve_diagnostics[sleeve]["last_evaluated_asof"] == "2026-10-30"

    # Week 1 (2026-11-06, 7 days later < 11d): 2W sleeves freeze exact prior sleeve weights
    w1 = build_weekly_plan(
        asof_date=date(2026, 11, 6),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
        cadences=SleeveCadences.hybrid_c(),
        previous_plan=w0,
    )
    assert w1.summary["cadence_profile"] == "hybrid_c"
    assert w1.sleeve_diagnostics["us_macro_allweather_v1"]["cadence_action"] == "HELD_2W_FREEZE"
    assert w1.sleeve_diagnostics["us_macro_allweather_v1"]["cadence_weeks"] == 2
    assert w1.sleeve_diagnostics["us_macro_allweather_v1"]["freeze_asof"] == "2026-11-06"
    assert w1.sleeve_diagnostics["us_macro_allweather_v1"]["last_evaluated_asof"] == "2026-10-30"

    assert w1.sleeve_diagnostics["hk_southbound_bluechip_v1"]["cadence_action"] == "HELD_2W_FREEZE"
    assert w1.sleeve_diagnostics["hk_southbound_bluechip_v1"]["cadence_weeks"] == 2
    assert w1.sleeve_diagnostics["hk_southbound_bluechip_v1"]["freeze_asof"] == "2026-11-06"
    assert w1.sleeve_diagnostics["hk_southbound_bluechip_v1"]["last_evaluated_asof"] == "2026-10-30"
    assert w1.sleeve_diagnostics["hk_southbound_bluechip_v1"]["cold_start"] is False

    # 1W sleeves evaluate every week
    assert w1.sleeve_diagnostics["us_sector_quality_mom_v1"]["cadence_action"] == "EVALUATED"
    assert w1.sleeve_diagnostics["us_sector_quality_mom_v1"]["last_evaluated_asof"] == "2026-11-06"
    assert w1.sleeve_diagnostics["hk_etf_southbound_rot_v1"]["cadence_action"] == "EVALUATED"
    assert w1.sleeve_diagnostics["hk_etf_southbound_rot_v1"]["last_evaluated_asof"] == "2026-11-06"

    # Frozen sleeves hold exact prior sleeve weights (<= 1e-8 rounding tolerance for 0.35 divisor)
    w0_bc = _sleeve_contrib_map(w0, "hk_southbound_bluechip_v1")
    w1_bc = _sleeve_contrib_map(w1, "hk_southbound_bluechip_v1")
    assert set(w1_bc) == set(w0_bc)
    for sym, exp_c in w0_bc.items():
        assert w1_bc[sym] == exp_c

    w0_mac = _sleeve_contrib_map(w0, "us_macro_allweather_v1")
    w1_mac = _sleeve_contrib_map(w1, "us_macro_allweather_v1")
    assert set(w1_mac) == set(w0_mac)
    for sym, exp_c in w0_mac.items():
        assert abs(w1_mac[sym] - exp_c) <= Decimal("0.00000001")

    # Week 2 (2026-11-13, 14 days after last_evaluated_asof=2026-10-30 >= 11d): all 4 evaluate!
    w2 = build_weekly_plan(
        asof_date=date(2026, 11, 13),
        refresh_data=False,
        us_cache_dir=US_CACHE_DIR,
        hk_cache_dir=HK_CACHE_DIR,
        cadences=SleeveCadences.hybrid_c(),
        previous_plan=w1,
    )
    for sleeve in (
        "us_macro_allweather_v1",
        "us_sector_quality_mom_v1",
        "hk_etf_southbound_rot_v1",
        "hk_southbound_bluechip_v1",
    ):
        assert w2.sleeve_diagnostics[sleeve]["cadence_action"] == "EVALUATED"
        assert w2.sleeve_diagnostics[sleeve]["last_evaluated_asof"] == "2026-11-13"
        assert "freeze_asof" not in w2.sleeve_diagnostics[sleeve]

    md = _format_plan_markdown(w1)
    assert "hybrid_c" in md
    assert "HELD_2W_FREEZE" in md
    assert "EVALUATED" in md


def test_cli_plan_hybrid_c_auto_picks_previous_plan_and_uniform_1w_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Step 1: Generate 2026-11-06 plan in tmp_path without --sync-ibkr-holdings
    assert (
        cli_main(
            [
                "plan",
                "--from-cache",
                "--asof",
                "2026-11-06",
                "--us-cache-dir",
                str(US_CACHE_DIR),
                "--hk-cache-dir",
                str(HK_CACHE_DIR),
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    plan_06 = load_weekly_plan(tmp_path / "plan_20261106.json")
    assert plan_06.summary["cadence_profile"] == "hybrid_c"
    capsys.readouterr()

    # Step 2: Generate 2026-11-13 plan in the same out_dir WITHOUT --sync-ibkr-holdings;
    # default --cadence hybrid-c MUST auto-load plan_20261106.json and freeze 2W sleeves!
    assert (
        cli_main(
            [
                "plan",
                "--from-cache",
                "--asof",
                "2026-11-13",
                "--us-cache-dir",
                str(US_CACHE_DIR),
                "--hk-cache-dir",
                str(HK_CACHE_DIR),
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    out_md = capsys.readouterr().out
    assert "HELD_2W_FREEZE" in out_md
    plan_13_hc = load_weekly_plan(tmp_path / "plan_20261113.json")
    assert plan_13_hc.summary["cadence_profile"] == "hybrid_c"
    assert plan_13_hc.sleeve_diagnostics["us_macro_allweather_v1"]["cadence_action"] == "HELD_2W_FREEZE"
    assert plan_13_hc.sleeve_diagnostics["hk_southbound_bluechip_v1"]["cadence_action"] == "HELD_2W_FREEZE"
    assert plan_13_hc.sleeve_diagnostics["us_sector_quality_mom_v1"]["cadence_action"] == "EVALUATED"
    assert plan_13_hc.sleeve_diagnostics["hk_etf_southbound_rot_v1"]["cadence_action"] == "EVALUATED"

    # Step 3: --cadence uniform-1w evaluates all 4 sleeves even when plan_20261106.json exists
    u1_dir = tmp_path / "uniform_1w"
    save_weekly_plan(plan_06, u1_dir / "plan_20261106.json")
    assert (
        cli_main(
            [
                "plan",
                "--from-cache",
                "--asof",
                "2026-11-13",
                "--cadence",
                "uniform-1w",
                "--us-cache-dir",
                str(US_CACHE_DIR),
                "--hk-cache-dir",
                str(HK_CACHE_DIR),
                "--out-dir",
                str(u1_dir),
            ]
        )
        == 0
    )
    plan_13_u1 = load_weekly_plan(u1_dir / "plan_20261113.json")
    assert plan_13_u1.summary["cadence_profile"] == "uniform_1w"
    for sleeve in (
        "us_macro_allweather_v1",
        "us_sector_quality_mom_v1",
        "hk_etf_southbound_rot_v1",
        "hk_southbound_bluechip_v1",
    ):
        assert plan_13_u1.sleeve_diagnostics[sleeve]["cadence_weeks"] == 1
        assert plan_13_u1.sleeve_diagnostics[sleeve]["cadence_action"] == "EVALUATED"
