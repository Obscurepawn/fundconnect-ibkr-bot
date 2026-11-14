"""Composite `35% / 35% / 20% / 10%` weekly plan generator and immutable JSON serializer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Literal

from ..config import (
    ETF_BOARD_LOTS,
    HK_DEFENSIVE_SYMBOL,
    US_DEFENSIVE_SYMBOL,
    US_EQUITY_STOCKS,
    US_MACRO_ETFS,
    SleeveWeights,
)
from ..data.calendars import (
    latest_completed_week_end,
    next_market_session,
    resolve_execution_session,
    us_open_in_hkt,
)
from ..data.cryptoracle import load_or_fetch_cryptoracle_btc
from ..data.indexone import (
    load_hk_eod_from_cache,
    load_us_dataset_from_cache,
    refresh_hk_indexone_eod,
    refresh_us_indexone_data,
)
from ..data.tushare import load_hk_tushare_from_cache, refresh_hk_tushare_data
from ..execution.hkex_rules import get_hk_board_lot, to_ibkr_contract_fields
from .hk_bluechip import compute_hk_bluechip_targets
from .hk_etf import compute_hk_etf_targets
from .us_macro import compute_us_macro_targets
from .us_quality_mom import compute_us_sqm_targets

WEIGHT_QUANT = Decimal("0.00000001")
PLAN_SCHEMA_ID = "fundconnect-ibkr-plan/v1"
_DEFAULT_SLEEVE_WEIGHTS = SleeveWeights()


def _q_weight(val: Decimal) -> Decimal:
    if val <= 0:
        return Decimal(0)
    return val.quantize(WEIGHT_QUANT, rounding=ROUND_DOWN)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class LiveSleeveState:
    """Optional live account holdings normalized per sleeve for week-to-week hysteresis."""

    us_macro_weights: Mapping[str, float] | None = None
    us_macro_prev_regime: frozenset[str] | None = None
    us_sqm_held_stocks: frozenset[str] = frozenset()
    hk_etf_weights: Mapping[str, float] | None = None
    hk_bluechip_weights: Mapping[str, float] | None = None
    hk_bluechip_prev_regime_scale: float | None = None

    @classmethod
    def from_live_portfolio_weights(
        cls,
        portfolio_weights: Mapping[str, float],
        *,
        sleeve_weights: SleeveWeights = _DEFAULT_SLEEVE_WEIGHTS,
        previous_plan: WeeklyPlan | None = None,
        us_macro_prev_regime: frozenset[str] | None = None,
        hk_bluechip_prev_regime_scale: float | None = None,
    ) -> LiveSleeveState:
        """Map live total-portfolio weights back into per-sleeve relative weights."""
        eff_prev_regime = us_macro_prev_regime
        eff_hk_bc_scale = hk_bluechip_prev_regime_scale
        if previous_plan is not None:
            mac_diag = previous_plan.sleeve_diagnostics.get("us_macro_allweather_v1", {})
            if eff_prev_regime is None and isinstance(mac_diag, Mapping) and "active_regime" in mac_diag:
                eff_prev_regime = frozenset(str(x) for x in mac_diag["active_regime"])
            bc_diag = previous_plan.sleeve_diagnostics.get("hk_southbound_bluechip_v1", {})
            if eff_hk_bc_scale is None and isinstance(bc_diag, Mapping) and "regime_scale" in bc_diag:
                eff_hk_bc_scale = float(bc_diag["regime_scale"])

        sqm_held = frozenset(
            s for s in US_EQUITY_STOCKS if portfolio_weights.get(s, 0.0) > 0.005
        )
        hk_etf_scale = float(sleeve_weights.hk_etf)
        hk_bc_scale = float(sleeve_weights.hk_bluechip)
        us_mac_scale = float(sleeve_weights.us_macro)
        us_sqm_scale = float(sleeve_weights.us_sqm)

        hk_etf_w = {
            s: portfolio_weights[s] / hk_etf_scale
            for s in ETF_BOARD_LOTS
            if portfolio_weights.get(s, 0.0) > 0.0 and hk_etf_scale > 0
        }
        hk_bc_w = {
            s: portfolio_weights[s] / hk_bc_scale
            for s, w in portfolio_weights.items()
            if s.endswith(".HK") and s not in ETF_BOARD_LOTS and w > 0.0 and hk_bc_scale > 0
        }
        # Reconstruct US Macro sleeve weights including shared SHY.US attribution
        us_mac_w: dict[str, float] | None = None
        if eff_prev_regime is not None and us_mac_scale > 0:
            us_mac_w = {
                s: portfolio_weights.get(s, 0.0) / us_mac_scale
                for s in US_MACRO_ETFS
                if s != "SHY.US" and portfolio_weights.get(s, 0.0) > 0.0
            }
            live_shy_total = portfolio_weights.get("SHY.US", 0.0)
            if live_shy_total > 0.0:
                prev_shy_target = (
                    previous_plan.target_map().get("SHY.US") if previous_plan is not None else None
                )
                if prev_shy_target is not None and prev_shy_target.portfolio_weight > 0:
                    mac_c = float(
                        prev_shy_target.sleeve_contributions.get("us_macro_allweather_v1", "0")
                    )
                    tot_c = float(prev_shy_target.portfolio_weight)
                    mac_shy_port = live_shy_total * (mac_c / tot_c) if tot_c > 0 else 0.0
                else:
                    sqm_eq_port = sum(portfolio_weights.get(s, 0.0) for s in US_EQUITY_STOCKS)
                    est_sqm_shy_port = max(0.0, 0.975 * us_sqm_scale - sqm_eq_port)
                    mac_shy_port = max(0.0, live_shy_total - est_sqm_shy_port)
                if mac_shy_port > 0.0:
                    us_mac_w["SHY.US"] = min(0.975, mac_shy_port / us_mac_scale)

        return cls(
            us_macro_weights=us_mac_w,
            us_macro_prev_regime=eff_prev_regime,
            us_sqm_held_stocks=sqm_held,
            hk_etf_weights=hk_etf_w or None,
            hk_bluechip_weights=hk_bc_w or None,
            hk_bluechip_prev_regime_scale=eff_hk_bc_scale,
        )


@dataclass(frozen=True)
class PlanTarget:
    symbol: str
    ibkr_symbol: str
    market: Literal["US", "HK"]
    exchange: str
    currency: Literal["USD", "HKD"]
    asset_class: Literal["etf", "stock"]
    portfolio_weight: Decimal
    sleeve_contributions: dict[str, str]
    ref_close: Decimal
    lot_size: int
    defensive_fallback: str
    is_defensive_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ibkr_symbol": self.ibkr_symbol,
            "market": self.market,
            "exchange": self.exchange,
            "currency": self.currency,
            "asset_class": self.asset_class,
            "portfolio_weight": f"{self.portfolio_weight:.8f}",
            "sleeve_contributions": dict(sorted(self.sleeve_contributions.items())),
            "ref_close": str(self.ref_close),
            "lot_size": self.lot_size,
            "defensive_fallback": self.defensive_fallback,
            "is_defensive_fallback": self.is_defensive_fallback,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PlanTarget:
        return cls(
            symbol=str(raw["symbol"]),
            ibkr_symbol=str(raw["ibkr_symbol"]),
            market="HK" if str(raw["market"]) == "HK" else "US",
            exchange=str(raw["exchange"]),
            currency="HKD" if str(raw["currency"]) == "HKD" else "USD",
            asset_class="etf" if str(raw["asset_class"]) == "etf" else "stock",
            portfolio_weight=Decimal(str(raw["portfolio_weight"])),
            sleeve_contributions={
                str(k): str(v) for k, v in dict(raw["sleeve_contributions"]).items()
            },
            ref_close=Decimal(str(raw["ref_close"])),
            lot_size=int(raw["lot_size"]),
            defensive_fallback=str(raw["defensive_fallback"]),
            is_defensive_fallback=bool(raw["is_defensive_fallback"]),
        )


@dataclass(frozen=True)
class WeeklyPlan:
    schema_id: str
    generated_at_utc: str
    asof_date: date
    scheduled_monday: date
    us_execution_session: date
    us_deferred_holiday: bool
    us_open_hkt: str
    hk_execution_session: date
    hk_deferred_holiday: bool
    sleeve_weights: dict[str, str]
    defensive_fallbacks: dict[str, str]
    defensive_ref_closes: dict[str, str]
    targets: tuple[PlanTarget, ...]
    sleeve_diagnostics: dict[str, Any]
    provenance_digests: dict[str, str]
    summary: dict[str, str]
    plan_sha256: str = field(default="")

    def targets_for_market(self, market: Literal["US", "HK"]) -> tuple[PlanTarget, ...]:
        return tuple(t for t in self.targets if t.market == market)

    def target_map(self) -> dict[str, PlanTarget]:
        return {t.symbol: t for t in self.targets}

    def to_dict_without_hash(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "generated_at_utc": self.generated_at_utc,
            "asof_date": self.asof_date.isoformat(),
            "scheduled_monday": self.scheduled_monday.isoformat(),
            "us_execution_session": self.us_execution_session.isoformat(),
            "us_deferred_holiday": self.us_deferred_holiday,
            "us_open_hkt": self.us_open_hkt,
            "hk_execution_session": self.hk_execution_session.isoformat(),
            "hk_deferred_holiday": self.hk_deferred_holiday,
            "sleeve_weights": dict(sorted(self.sleeve_weights.items())),
            "defensive_fallbacks": dict(sorted(self.defensive_fallbacks.items())),
            "defensive_ref_closes": dict(sorted(self.defensive_ref_closes.items())),
            "targets": [t.to_dict() for t in self.targets],
            "sleeve_diagnostics": self.sleeve_diagnostics,
            "provenance_digests": dict(sorted(self.provenance_digests.items())),
            "summary": dict(sorted(self.summary.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        base = self.to_dict_without_hash()
        digest = self.plan_sha256 or hashlib.sha256(_canonical_json_bytes(base)).hexdigest()
        return {**base, "plan_sha256": digest}


_DEFAULT_LIVE_STATE = LiveSleeveState()


def build_weekly_plan(
    *,
    asof_date: date | None = None,
    refresh_data: bool = False,
    data_dir: Path = Path("data"),
    us_cache_dir: Path | None = None,
    hk_cache_dir: Path | None = None,
    sleeve_weights: SleeveWeights = _DEFAULT_SLEEVE_WEIGHTS,
    live_state: LiveSleeveState = _DEFAULT_LIVE_STATE,
    generated_at_utc: datetime | None = None,
) -> WeeklyPlan:
    """Compute immutable Monday execution plan across all 4 strategies."""
    eff_us_cache = us_cache_dir or data_dir
    eff_hk_cache = hk_cache_dir or data_dir

    target_asof = asof_date or latest_completed_week_end("US", datetime.now(UTC).date())

    if refresh_data:
        us_data = refresh_us_indexone_data(data_dir, end_date=target_asof)
        crypto_rows, crypto_sha = load_or_fetch_cryptoracle_btc(
            data_dir, end_date=target_asof, refresh=True
        )
        hk_ts = refresh_hk_tushare_data(data_dir, end_date=target_asof)
        hk_sessions = tuple(d for d in hk_ts.open_dates if date(2023, 6, 1) <= d <= target_asof)
        hk_bars, hk_eod_sha = refresh_hk_indexone_eod(
            data_dir, hk_sessions=hk_sessions, end_date=target_asof
        )
    else:
        us_data = load_us_dataset_from_cache(eff_us_cache, max_date=target_asof)
        crypto_rows, crypto_sha = load_or_fetch_cryptoracle_btc(
            eff_us_cache, end_date=target_asof, refresh=False
        )
        hk_ts = load_hk_tushare_from_cache(eff_hk_cache, max_date=target_asof)
        hk_sessions = tuple(d for d in hk_ts.open_dates if date(2023, 6, 1) <= d <= target_asof)
        hk_bars, hk_eod_sha = load_hk_eod_from_cache(
            eff_hk_cache, hk_sessions=hk_sessions, max_date=target_asof
        )

    hk_open_set = frozenset(hk_ts.open_dates)
    after_hk = [d for d in hk_ts.open_dates if d > target_asof]
    hk_next_sess = after_hk[0] if after_hk else next_market_session("HK", target_asof)

    # Resolve scheduled Monday (+ automatic holiday deferral per exchange)
    days_until_monday = (7 - target_asof.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    scheduled_monday = target_asof + timedelta(days=days_until_monday)
    us_exec_day, us_deferred = resolve_execution_session("US", scheduled_monday)
    hk_exec_day, hk_deferred = resolve_execution_session(
        "HK", scheduled_monday, hk_open_dates=hk_open_set
    )

    # 1. Run 4 sleeve engines
    w_us_macro, diag_us_macro = compute_us_macro_targets(
        target_asof,
        us_data.eod_by_symbol,
        crypto_rows,
        actual_sleeve_weights=live_state.us_macro_weights,
        prev_active_regime=live_state.us_macro_prev_regime,
    )
    w_us_sqm, diag_us_sqm = compute_us_sqm_targets(
        target_asof,
        us_data.eod_by_symbol,
        us_data.fundamentals_rows,
        held_stocks=live_state.us_sqm_held_stocks,
    )
    w_hk_etf, diag_hk_etf = compute_hk_etf_targets(
        target_asof,
        hk_sessions,
        hk_next_sess,
        hk_bars,
        hk_ts.hk_hold_rows,
        hk_ts.ggt_rows,
        actual_sleeve_weights=live_state.hk_etf_weights,
    )
    w_hk_bc, diag_hk_bc = compute_hk_bluechip_targets(
        target_asof,
        hk_sessions,
        hk_next_sess,
        hk_bars,
        hk_ts.hk_hold_rows,
        hk_ts.ggt_rows,
        actual_sleeve_weights=live_state.hk_bluechip_weights,
        prev_regime_scale=live_state.hk_bluechip_prev_regime_scale,
    )

    # 2. Combine with 35% / 35% / 20% / 10% sleeve weights
    combined_raw: dict[str, Decimal] = {}
    contribs: dict[str, dict[str, str]] = {}

    sleeve_outputs = (
        ("us_macro_allweather_v1", sleeve_weights.us_macro, w_us_macro),
        ("us_sector_quality_mom_v1", sleeve_weights.us_sqm, w_us_sqm),
        ("hk_etf_southbound_rot_v1", sleeve_weights.hk_etf, w_hk_etf),
        ("hk_southbound_bluechip_v1", sleeve_weights.hk_bluechip, w_hk_bc),
    )
    for sleeve_name, s_mult, s_map in sleeve_outputs:
        for sym, sw in sorted(s_map.items()):
            contrib = s_mult * sw
            combined_raw[sym] = combined_raw.get(sym, Decimal(0)) + contrib
            contribs.setdefault(sym, {})[sleeve_name] = f"{_q_weight(contrib):.8f}"

    targets_list: list[PlanTarget] = []
    for sym in sorted(combined_raw):
        pw = _q_weight(combined_raw[sym])
        if pw <= 0:
            continue
        ibkr_sym, exch, ccy = to_ibkr_contract_fields(sym)
        if ccy == "USD":
            mkt: Literal["US", "HK"] = "US"
            currency_lit: Literal["USD", "HKD"] = "USD"
            lot = 1
            ref_px = Decimal(str(us_data.eod_by_symbol[sym][-1].raw_close))
            is_etf = sym in US_MACRO_ETFS
            fallback = US_DEFENSIVE_SYMBOL
        else:
            mkt = "HK"
            currency_lit = "HKD"
            lot = get_hk_board_lot(sym, hk_basic_lots=hk_ts.board_lots)
            ref_px = Decimal(str(hk_bars[sym][target_asof].raw_close))
            is_etf = sym in ETF_BOARD_LOTS
            fallback = HK_DEFENSIVE_SYMBOL

        targets_list.append(
            PlanTarget(
                symbol=sym,
                ibkr_symbol=ibkr_sym,
                market=mkt,
                exchange=exch,
                currency=currency_lit,
                asset_class="etf" if is_etf else "stock",
                portfolio_weight=pw,
                sleeve_contributions=contribs[sym],
                ref_close=ref_px,
                lot_size=lot,
                defensive_fallback=fallback,
                is_defensive_fallback=(sym in {US_DEFENSIVE_SYMBOL, HK_DEFENSIVE_SYMBOL}),
            )
        )

    us_sum = sum((t.portfolio_weight for t in targets_list if t.market == "US"), Decimal(0))
    hk_sum = sum((t.portfolio_weight for t in targets_list if t.market == "HK"), Decimal(0))
    total_invested = us_sum + hk_sum

    shy_ref = Decimal(str(us_data.eod_by_symbol[US_DEFENSIVE_SYMBOL][-1].raw_close))
    hk_mm_ref = Decimal(str(hk_bars[HK_DEFENSIVE_SYMBOL][target_asof].raw_close))
    gen_ts = (generated_at_utc or datetime.now(UTC)).isoformat()

    draft = WeeklyPlan(
        schema_id=PLAN_SCHEMA_ID,
        generated_at_utc=gen_ts,
        asof_date=target_asof,
        scheduled_monday=scheduled_monday,
        us_execution_session=us_exec_day,
        us_deferred_holiday=us_deferred,
        us_open_hkt=us_open_in_hkt(us_exec_day).isoformat(),
        hk_execution_session=hk_exec_day,
        hk_deferred_holiday=hk_deferred,
        sleeve_weights={
            "us_macro_allweather_v1": f"{sleeve_weights.us_macro:.4f}",
            "us_sector_quality_mom_v1": f"{sleeve_weights.us_sqm:.4f}",
            "hk_etf_southbound_rot_v1": f"{sleeve_weights.hk_etf:.4f}",
            "hk_southbound_bluechip_v1": f"{sleeve_weights.hk_bluechip:.4f}",
        },
        defensive_fallbacks={"US": US_DEFENSIVE_SYMBOL, "HK": HK_DEFENSIVE_SYMBOL},
        defensive_ref_closes={
            US_DEFENSIVE_SYMBOL: str(shy_ref),
            HK_DEFENSIVE_SYMBOL: str(hk_mm_ref),
        },
        targets=tuple(targets_list),
        sleeve_diagnostics={
            "us_macro_allweather_v1": diag_us_macro,
            "us_sector_quality_mom_v1": diag_us_sqm,
            "hk_etf_southbound_rot_v1": diag_hk_etf,
            "hk_southbound_bluechip_v1": diag_hk_bc,
        },
        provenance_digests={
            "us_eod_sha256": us_data.eod_sha256,
            "us_fundamentals_sha256": us_data.fundamentals_sha256,
            "btc_cryptoracle_sha256": crypto_sha,
            "hk_eod_sha256": hk_eod_sha,
            **hk_ts.digests,
        },
        summary={
            "us_invested_weight": f"{us_sum:.8f}",
            "us_cash_buffer_weight": f"{(sleeve_weights.us_total - us_sum):.8f}",
            "hk_invested_weight": f"{hk_sum:.8f}",
            "hk_cash_buffer_weight": f"{(sleeve_weights.hk_total - hk_sum):.8f}",
            "total_invested_weight": f"{total_invested:.8f}",
            "total_cash_buffer_weight": f"{(Decimal('1.00') - total_invested):.8f}",
            "target_instrument_count": str(len(targets_list)),
        },
    )
    digest = hashlib.sha256(_canonical_json_bytes(draft.to_dict_without_hash())).hexdigest()
    return WeeklyPlan(**{**draft.__dict__, "plan_sha256": digest})


def save_weekly_plan(plan: WeeklyPlan, out_path: Path) -> Path:
    """Persist immutable hash-stamped `plan_<YYYYMMDD>.json`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(plan.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return out_path


def load_weekly_plan(path: Path) -> WeeklyPlan:
    """Load and cryptographically verify a saved `plan_<YYYYMMDD>.json`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_id") != PLAN_SCHEMA_ID:
        raise ValueError(f"Unsupported plan schema_id: {raw.get('schema_id')}")
    claimed_sha = str(raw.get("plan_sha256") or "")
    body = {k: v for k, v in raw.items() if k != "plan_sha256"}
    actual_sha = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if claimed_sha != actual_sha:
        raise ValueError(
            f"Plan SHA-256 verification failed for {path}: expected {claimed_sha}, got {actual_sha}"
        )
    return WeeklyPlan(
        schema_id=str(raw["schema_id"]),
        generated_at_utc=str(raw["generated_at_utc"]),
        asof_date=date.fromisoformat(str(raw["asof_date"])),
        scheduled_monday=date.fromisoformat(str(raw["scheduled_monday"])),
        us_execution_session=date.fromisoformat(str(raw["us_execution_session"])),
        us_deferred_holiday=bool(raw["us_deferred_holiday"]),
        us_open_hkt=str(raw["us_open_hkt"]),
        hk_execution_session=date.fromisoformat(str(raw["hk_execution_session"])),
        hk_deferred_holiday=bool(raw["hk_deferred_holiday"]),
        sleeve_weights={str(k): str(v) for k, v in dict(raw["sleeve_weights"]).items()},
        defensive_fallbacks={
            str(k): str(v) for k, v in dict(raw["defensive_fallbacks"]).items()
        },
        defensive_ref_closes={
            str(k): str(v) for k, v in dict(raw["defensive_ref_closes"]).items()
        },
        targets=tuple(PlanTarget.from_dict(item) for item in raw["targets"]),
        sleeve_diagnostics=dict(raw["sleeve_diagnostics"]),
        provenance_digests={
            str(k): str(v) for k, v in dict(raw["provenance_digests"]).items()
        },
        summary={str(k): str(v) for k, v in dict(raw["summary"]).items()},
        plan_sha256=claimed_sha,
    )
