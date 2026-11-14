"""Core Gap, News-Shock, Spread/Halt Guard & Two-Phase Zero-Margin Execution State Machine."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..config import (
    ETF_BOARD_LOTS,
    HK_DEFENSIVE_SYMBOL,
    US_DEFENSIVE_SYMBOL,
    US_MACRO_ETFS,
    ExecutionPolicyConfig,
)
from .broker import (
    BrokerAdapter,
    CashSnap,
    CurrencyCode,
    FillReport,
    FxConversionReport,
    OpenOrderSnap,
    OrderSide,
    OrderSpec,
    OrderStyle,
    PositionSnap,
    Quote,
)
from .hkex_rules import (
    floor_to_lot,
    get_hk_board_lot,
    round_market_price,
    split_qty_into_lot_slices,
    to_ibkr_contract_fields,
)

if TYPE_CHECKING:
    from ..strategies.composite import PlanTarget, WeeklyPlan

type MarketCode = Literal["US", "HK"]
_DEFAULT_POLICY_CONFIG = ExecutionPolicyConfig()


class ActionCode(StrEnum):
    BUY_NORMAL = "BUY_NORMAL"
    BUY_STAGED_50 = "BUY_STAGED_50"
    SKIP_GAP_UP = "SKIP_GAP_UP"
    SKIP_ADVERSE_GAP_DOWN = "SKIP_ADVERSE_GAP_DOWN"
    BUY_DEFENSIVE_FALLBACK = "BUY_DEFENSIVE_FALLBACK"
    SELL_NORMAL = "SELL_NORMAL"
    SELL_SLICED_TWAP = "SELL_SLICED_TWAP"
    HOLD_FLASH_CRASH_GUARD = "HOLD_FLASH_CRASH_GUARD"
    HALTED_OR_NO_QUOTE = "HALTED_OR_NO_QUOTE"
    SKIP_BELOW_MIN_NOTIONAL = "SKIP_BELOW_MIN_NOTIONAL"
    SKIP_ALREADY_WORKING_OR_DONE = "SKIP_ALREADY_WORKING_OR_DONE"
    SKIP_CASH_CAP = "SKIP_CASH_CAP"
    HOLD_AT_TARGET = "HOLD_AT_TARGET"


@dataclass(frozen=True)
class InstrumentDecision:
    symbol: str
    market: MarketCode
    currency: CurrencyCode
    action: ActionCode
    current_qty: int
    working_open_qty: int
    desired_total_qty: int
    delta_qty: int
    ref_close: float
    live_mid: float | None
    gap_pct: float | None
    spread_bps: float | None
    wide_spread: bool
    base_target_notional: float
    effective_target_notional: float
    redirected_to_defensive: float
    order_style: OrderStyle | None
    limit_price: Decimal | None
    slice_quantities: tuple[int, ...]
    note: str


@dataclass(frozen=True)
class ExecutionBatchPlan:
    market: MarketCode
    currency: CurrencyCode
    execution_session: str
    total_equity_usd: float
    total_equity_hkd: float
    usdhkd_rate: float
    redirected_defensive_notional: float
    decisions: tuple[InstrumentDecision, ...]
    phase_a_sells: tuple[OrderSpec, ...]
    phase_b_buys: tuple[OrderSpec, ...]
    alerts: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionState:
    plan_sha256: str
    execution_session: str
    market: MarketCode
    completed_intents: dict[str, dict[str, Any]]
    updated_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_sha256": self.plan_sha256,
            "execution_session": self.execution_session,
            "market": self.market,
            "completed_intents": dict(sorted(self.completed_intents.items())),
            "updated_at_utc": self.updated_at_utc,
        }


@dataclass(frozen=True)
class ExecutionReport:
    batch: ExecutionBatchPlan
    phase_a_fills: tuple[FillReport, ...]
    fx_report: FxConversionReport | None
    capped_phase_b_buys: tuple[OrderSpec, ...]
    phase_b_fills: tuple[FillReport, ...]
    spendable_cash_before_buys: float
    alerts: tuple[str, ...]
    state_path: Path | None


def load_execution_state(path: Path, *, expected_plan_sha256: str) -> ExecutionState | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if str(raw.get("plan_sha256") or "") != expected_plan_sha256:
        return None
    mkt: MarketCode = "HK" if str(raw.get("market")) == "HK" else "US"
    return ExecutionState(
        plan_sha256=expected_plan_sha256,
        execution_session=str(raw["execution_session"]),
        market=mkt,
        completed_intents={
            str(k): dict(v) for k, v in dict(raw.get("completed_intents") or {}).items()
        },
        updated_at_utc=str(raw.get("updated_at_utc") or ""),
    )


def save_execution_state(state: ExecutionState, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _make_intent_id(
    plan_sha256: str,
    exec_session: str,
    market: MarketCode,
    symbol: str,
    side: OrderSide,
    slice_index: int,
) -> str:
    raw = f"{plan_sha256}:{exec_session}:{market}:{symbol}:{side}:{slice_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_portfolio_equity_usd(
    positions: Mapping[str, PositionSnap],
    cash: Mapping[CurrencyCode, CashSnap],
    quotes: Mapping[str, Quote],
    usdhkd_rate: float,
) -> float:
    """Compute live mark-to-market total account equity in USD across USD + HKD sleeves."""
    usd_cash = cash["USD"].total_cash if "USD" in cash else 0.0
    hkd_cash = cash["HKD"].total_cash if "HKD" in cash else 0.0
    eq_usd = usd_cash + (hkd_cash / usdhkd_rate)
    for sym, pos in positions.items():
        if pos.quantity == 0:
            continue
        q = quotes.get(sym)
        mark = q.mid if (q is not None and q.is_valid) else pos.market_price
        mv = pos.quantity * mark
        eq_usd += mv if pos.currency == "USD" else (mv / usdhkd_rate)
    return max(0.0, eq_usd)


def _select_style_and_limit(
    *,
    symbol: str,
    market: MarketCode,
    side: OrderSide,
    quote: Quote,
    ref_close: float,
    wide_spread: bool,
    patient_override: bool,
    flash_crash_floor: bool,
    use_adaptive: bool,
    config: ExecutionPolicyConfig,
) -> tuple[OrderStyle, Decimal]:
    mid = quote.mid
    if flash_crash_floor:
        raw_floor = ref_close * (1.0 + config.sell_flash_crash_threshold)
        return (
            "PROTECTIVE_FLOOR_LIMIT",
            round_market_price(raw_floor, "SELL", market=market, symbol=symbol),
        )
    if wide_spread or patient_override:
        style: OrderStyle = (
            "ADAPTIVE_PATIENT" if (use_adaptive and market == "US") else "PASSIVE_MID_LIMIT"
        )
        return style, round_market_price(mid, side, market=market, symbol=symbol)

    style = "ADAPTIVE_NORMAL" if (use_adaptive and market == "US") else "MARKETABLE_LIMIT"
    if side == "BUY":
        max_cap = mid * (1.0 + config.max_slippage_bps / 10_000.0)
        init_cap = mid * (1.0 + config.initial_limit_bps / 10_000.0)
        raw_px = min(quote.ask, init_cap, max_cap)
        return style, round_market_price(raw_px, "BUY", market=market, symbol=symbol)

    min_cap = mid * (1.0 - config.max_slippage_bps / 10_000.0)
    init_floor = mid * (1.0 - config.initial_limit_bps / 10_000.0)
    raw_px = max(quote.bid, init_floor, min_cap)
    return style, round_market_price(raw_px, "SELL", market=market, symbol=symbol)


def compute_walked_limit_price(
    spec: OrderSpec,
    live_quote: Quote | None,
    step_num: int,
    config: ExecutionPolicyConfig,
) -> Decimal | None:
    """Compute bounded Peg-to-Mid walk-limit price for step `1..max_walk_steps`.

    Returns `None` if repricing is disallowed (protective floor, halted quote, wide spread,
    or already at the max slippage cap).
    """
    if step_num < 1 or step_num > config.max_walk_steps:
        return None
    if spec.style == "PROTECTIVE_FLOOR_LIMIT":
        return None
    if live_quote is None or not live_quote.is_valid:
        return None

    mkt: MarketCode = "HK" if spec.currency == "HKD" else "US"
    max_sp = config.max_spread_bps_hk if mkt == "HK" else config.max_spread_bps_us
    if live_quote.spread_bps > max_sp:
        return None

    bps = min(
        config.max_slippage_bps,
        config.initial_limit_bps + step_num * config.walk_step_bps,
    )
    if spec.side == "BUY":
        hard_cap = spec.arrival_mid * (1.0 + config.max_slippage_bps / 10_000.0)
        raw_new = min(
            live_quote.ask,
            spec.arrival_mid * (1.0 + bps / 10_000.0),
            hard_cap,
        )
        rounded = round_market_price(raw_new, "BUY", market=mkt, symbol=spec.symbol)
        return rounded if rounded > spec.limit_price else None

    hard_floor = spec.arrival_mid * (1.0 - config.max_slippage_bps / 10_000.0)
    raw_new = max(
        live_quote.bid,
        spec.arrival_mid * (1.0 - bps / 10_000.0),
        hard_floor,
    )
    rounded = round_market_price(raw_new, "SELL", market=mkt, symbol=spec.symbol)
    return rounded if rounded < spec.limit_price else None


def build_execution_batch(
    plan: WeeklyPlan,
    *,
    market: MarketCode,
    positions: Mapping[str, PositionSnap],
    cash: Mapping[CurrencyCode, CashSnap],
    open_orders: Sequence[OpenOrderSnap],
    quotes: Mapping[str, Quote],
    usdhkd_rate: float = 7.80,
    use_adaptive: bool = True,
    persisted_state: ExecutionState | None = None,
    config: ExecutionPolicyConfig = _DEFAULT_POLICY_CONFIG,
) -> ExecutionBatchPlan:
    """Evaluate gap/shock tiers, wide-spread/halt guards, open-order offsets, and 2-phase orders."""
    ccy: CurrencyCode = "HKD" if market == "HK" else "USD"
    defensive_sym = HK_DEFENSIVE_SYMBOL if market == "HK" else US_DEFENSIVE_SYMBOL
    exec_session = (
        plan.hk_execution_session.isoformat()
        if market == "HK"
        else plan.us_execution_session.isoformat()
    )
    min_notional = config.min_trade_hkd if market == "HK" else config.min_trade_usd
    max_spread_bps = config.max_spread_bps_hk if market == "HK" else config.max_spread_bps_us

    total_eq_usd = compute_portfolio_equity_usd(positions, cash, quotes, usdhkd_rate)
    total_eq_hkd = total_eq_usd * usdhkd_rate
    total_eq_local = total_eq_hkd if market == "HK" else total_eq_usd

    mkt_targets: dict[str, PlanTarget] = {
        t.symbol: t for t in plan.targets_for_market(market)
    }
    held_in_market = {
        sym: pos for sym, pos in positions.items() if pos.currency == ccy and pos.quantity > 0
    }

    # Index already-working open orders by (symbol, side)
    working_by_sym_side: dict[tuple[str, OrderSide], int] = {}
    for oo in open_orders:
        if oo.remaining_qty > 0:
            key = (oo.symbol, oo.side)
            working_by_sym_side[key] = working_by_sym_side.get(key, 0) + oo.remaining_qty

    completed_intents = persisted_state.completed_intents if persisted_state else {}

    all_non_def_syms = sorted(
        (set(mkt_targets) | set(held_in_market)) - {defensive_sym}
    )

    decisions: list[InstrumentDecision] = []
    phase_a_sells: list[OrderSpec] = []
    phase_b_buys: list[OrderSpec] = []
    alerts: list[str] = []
    redirected_defensive = 0.0

    def _meta_for_symbol(sym: str) -> tuple[str, str, Literal["etf", "stock"], int, float]:
        if sym in mkt_targets:
            t = mkt_targets[sym]
            return t.ibkr_symbol, t.exchange, t.asset_class, t.lot_size, float(t.ref_close)
        ibkr_s, exch, _ = to_ibkr_contract_fields(sym)
        if market == "HK":
            is_etf = sym in ETF_BOARD_LOTS
            try:
                lot = get_hk_board_lot(sym)
            except KeyError:
                lot = 100
        else:
            is_etf = sym in US_MACRO_ETFS or sym.endswith("GOV.US")
            lot = 1
        q = quotes.get(sym)
        ref_c = (
            float(plan.defensive_ref_closes[sym])
            if sym in plan.defensive_ref_closes
            else (q.prev_close if q and q.prev_close > 0 else (held_in_market[sym].market_price if sym in held_in_market else 0.0))
        )
        return ibkr_s, exch, ("etf" if is_etf else "stock"), lot, ref_c

    def _emit_orders_and_decision(
        *,
        sym: str,
        action: ActionCode,
        cur_qty: int,
        desired_qty: int,
        base_notional: float,
        eff_notional: float,
        redirected: float,
        q: Quote | None,
        ref_c: float,
        lot: int,
        ibkr_s: str,
        exch: str,
        asset_cls: Literal["etf", "stock"],
        note: str,
    ) -> None:
        if q is None or not q.is_valid:
            decisions.append(
                InstrumentDecision(
                    symbol=sym,
                    market=market,
                    currency=ccy,
                    action=ActionCode.HALTED_OR_NO_QUOTE,
                    current_qty=cur_qty,
                    working_open_qty=0,
                    desired_total_qty=cur_qty,
                    delta_qty=0,
                    ref_close=ref_c,
                    live_mid=None,
                    gap_pct=None,
                    spread_bps=None,
                    wide_spread=False,
                    base_target_notional=base_notional,
                    effective_target_notional=eff_notional,
                    redirected_to_defensive=0.0,
                    order_style=None,
                    limit_price=None,
                    slice_quantities=(),
                    note=note,
                )
            )
            return

        mid = q.mid
        sp_bps = q.spread_bps
        wide = sp_bps > max_spread_bps
        gap = (mid - ref_c) / ref_c if ref_c > 0 else 0.0

        raw_delta = desired_qty - cur_qty
        if action in (
            ActionCode.SKIP_GAP_UP,
            ActionCode.SKIP_ADVERSE_GAP_DOWN,
            ActionCode.HOLD_AT_TARGET,
        ) or (raw_delta == 0 and action != ActionCode.HOLD_FLASH_CRASH_GUARD):
            decisions.append(
                InstrumentDecision(
                    symbol=sym,
                    market=market,
                    currency=ccy,
                    action=action if raw_delta == 0 and action != ActionCode.BUY_NORMAL else (ActionCode.HOLD_AT_TARGET if raw_delta == 0 else action),
                    current_qty=cur_qty,
                    working_open_qty=0,
                    desired_total_qty=desired_qty,
                    delta_qty=0,
                    ref_close=ref_c,
                    live_mid=mid,
                    gap_pct=gap,
                    spread_bps=sp_bps,
                    wide_spread=wide,
                    base_target_notional=base_notional,
                    effective_target_notional=eff_notional,
                    redirected_to_defensive=redirected,
                    order_style=None,
                    limit_price=None,
                    slice_quantities=(),
                    note=note,
                )
            )
            return

        side: OrderSide = (
            "SELL"
            if (raw_delta < 0 or action == ActionCode.HOLD_FLASH_CRASH_GUARD)
            else "BUY"
        )
        working_qty = working_by_sym_side.get((sym, side), 0)
        gross_qty = cur_qty - desired_qty if side == "SELL" else desired_qty - cur_qty
        net_qty = (
            max(0, gross_qty - working_qty)
            if side == "SELL"
            else floor_to_lot(max(0, gross_qty - working_qty), lot)
        )

        if net_qty == 0:
            decisions.append(
                InstrumentDecision(
                    symbol=sym,
                    market=market,
                    currency=ccy,
                    action=ActionCode.SKIP_ALREADY_WORKING_OR_DONE,
                    current_qty=cur_qty,
                    working_open_qty=working_qty,
                    desired_total_qty=desired_qty,
                    delta_qty=0,
                    ref_close=ref_c,
                    live_mid=mid,
                    gap_pct=gap,
                    spread_bps=sp_bps,
                    wide_spread=wide,
                    base_target_notional=base_notional,
                    effective_target_notional=eff_notional,
                    redirected_to_defensive=redirected,
                    order_style=None,
                    limit_price=None,
                    slice_quantities=(),
                    note=f"Already covered by {working_qty} open {side} shares.",
                )
            )
            return

        # Min notional churn filter (never blocks full position liquidation to 0 shares)
        is_full_exit = side == "SELL" and desired_qty == 0
        if not is_full_exit and (net_qty * mid) < min_notional:
            decisions.append(
                InstrumentDecision(
                    symbol=sym,
                    market=market,
                    currency=ccy,
                    action=ActionCode.SKIP_BELOW_MIN_NOTIONAL,
                    current_qty=cur_qty,
                    working_open_qty=working_qty,
                    desired_total_qty=cur_qty,
                    delta_qty=0,
                    ref_close=ref_c,
                    live_mid=mid,
                    gap_pct=gap,
                    spread_bps=sp_bps,
                    wide_spread=wide,
                    base_target_notional=base_notional,
                    effective_target_notional=eff_notional,
                    redirected_to_defensive=redirected,
                    order_style=None,
                    limit_price=None,
                    slice_quantities=(),
                    note=f"Trade notional {net_qty * mid:,.2f} {ccy} < min {min_notional:,.0f} {ccy}.",
                )
            )
            return

        patient_flag = action in (ActionCode.BUY_STAGED_50, ActionCode.SELL_SLICED_TWAP)
        flash_floor = action == ActionCode.HOLD_FLASH_CRASH_GUARD
        style, lmt_px = _select_style_and_limit(
            symbol=sym,
            market=market,
            side=side,
            quote=q,
            ref_close=ref_c,
            wide_spread=wide,
            patient_override=patient_flag,
            flash_crash_floor=flash_floor,
            use_adaptive=use_adaptive,
            config=config,
        )
        if wide:
            alerts.append(
                f"[WIDE_SPREAD] {sym}: spread={sp_bps:.1f} bps > {max_spread_bps:.0f} bps; "
                f"switched {side} to passive mid {lmt_px} ({style})."
            )

        num_slices = config.severe_sell_slices if action == ActionCode.SELL_SLICED_TWAP else 1
        raw_slices = split_qty_into_lot_slices(net_qty, lot, num_slices)
        active_slices: list[int] = []
        phase_lit: Literal["PHASE_A_SELL", "PHASE_B_BUY"] = (
            "PHASE_A_SELL" if side == "SELL" else "PHASE_B_BUY"
        )

        for idx_zero, sq in enumerate(raw_slices):
            s_idx = idx_zero + 1
            intent = _make_intent_id(plan.plan_sha256, exec_session, market, sym, side, s_idx)
            if intent in completed_intents:
                continue
            active_slices.append(sq)
            spec = OrderSpec(
                intent_id=intent,
                symbol=sym,
                ibkr_symbol=ibkr_s,
                exchange=exch,
                currency=ccy,
                asset_class=asset_cls,
                side=side,
                quantity=sq,
                lot_size=lot,
                limit_price=lmt_px,
                arrival_mid=mid,
                style=style,
                phase=phase_lit,
                slice_index=s_idx,
                slice_count=len(raw_slices),
                reason=action.value,
            )
            if side == "SELL":
                phase_a_sells.append(spec)
            else:
                phase_b_buys.append(spec)

        if not active_slices:
            decisions.append(
                InstrumentDecision(
                    symbol=sym,
                    market=market,
                    currency=ccy,
                    action=ActionCode.SKIP_ALREADY_WORKING_OR_DONE,
                    current_qty=cur_qty,
                    working_open_qty=working_qty,
                    desired_total_qty=desired_qty,
                    delta_qty=0,
                    ref_close=ref_c,
                    live_mid=mid,
                    gap_pct=gap,
                    spread_bps=sp_bps,
                    wide_spread=wide,
                    base_target_notional=base_notional,
                    effective_target_notional=eff_notional,
                    redirected_to_defensive=redirected,
                    order_style=None,
                    limit_price=None,
                    slice_quantities=(),
                    note="Intent already recorded in persisted execution state.",
                )
            )
            return

        signed_delta = -sum(active_slices) if side == "SELL" else sum(active_slices)
        decisions.append(
            InstrumentDecision(
                symbol=sym,
                market=market,
                currency=ccy,
                action=action,
                current_qty=cur_qty,
                working_open_qty=working_qty,
                desired_total_qty=desired_qty,
                delta_qty=signed_delta,
                ref_close=ref_c,
                live_mid=mid,
                gap_pct=gap,
                spread_bps=sp_bps,
                wide_spread=wide,
                base_target_notional=base_notional,
                effective_target_notional=eff_notional,
                redirected_to_defensive=redirected,
                order_style=style,
                limit_price=lmt_px,
                slice_quantities=tuple(active_slices),
                note=note,
            )
        )

    # 1. Evaluate all non-defensive holdings and targets first
    for sym in all_non_def_syms:
        ibkr_s, exch, asset_cls, lot, ref_c = _meta_for_symbol(sym)
        cur_qty = held_in_market[sym].quantity if sym in held_in_market else 0
        base_notional = (
            float(mkt_targets[sym].portfolio_weight) * total_eq_local
            if sym in mkt_targets
            else 0.0
        )
        q = quotes.get(sym)

        if q is None or not q.is_valid:
            alerts.append(
                f"[HALTED_OR_NO_QUOTE] {sym}: invalid or missing Level-1 quote; skipping safely."
            )
            _emit_orders_and_decision(
                sym=sym,
                action=ActionCode.HALTED_OR_NO_QUOTE,
                cur_qty=cur_qty,
                desired_qty=cur_qty,
                base_notional=base_notional,
                eff_notional=base_notional,
                redirected=0.0,
                q=None,
                ref_c=ref_c,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note="Invalid or halted quote (bid<=0, ask<=0, or ask<bid).",
            )
            continue

        mid = q.mid
        eff_ref = ref_c if ref_c > 0 else (q.prev_close if q.prev_close > 0 else mid)
        gap_pct = (mid - eff_ref) / eff_ref if eff_ref > 0 else 0.0
        ideal_qty_mid = floor_to_lot(base_notional / mid, lot) if base_notional > 0 else 0

        # Branch A: SELL / Trim / Full Exit
        if ideal_qty_mid < cur_qty:
            if gap_pct < config.sell_flash_crash_threshold:
                alerts.append(
                    f"[FLASH_CRASH_GUARD] {sym}: gap={gap_pct:+.2%} < {config.sell_flash_crash_threshold:+.1%}; "
                    f"holding with protective floor limit at -12% of ref_close."
                )
                _emit_orders_and_decision(
                    sym=sym,
                    action=ActionCode.HOLD_FLASH_CRASH_GUARD,
                    cur_qty=cur_qty,
                    desired_qty=ideal_qty_mid,
                    base_notional=base_notional,
                    eff_notional=base_notional,
                    redirected=0.0,
                    q=q,
                    ref_c=eff_ref,
                    lot=lot,
                    ibkr_s=ibkr_s,
                    exch=exch,
                    asset_cls=asset_cls,
                    note=f"Flash-crash guard ({gap_pct:+.2%}); protective floor limit only.",
                )
            elif gap_pct < config.sell_severe_gap_threshold:
                alerts.append(
                    f"[SELL_SLICED_TWAP] {sym}: severe gap-down {gap_pct:+.2%}; "
                    f"splitting sell into {config.severe_sell_slices} patient slices."
                )
                _emit_orders_and_decision(
                    sym=sym,
                    action=ActionCode.SELL_SLICED_TWAP,
                    cur_qty=cur_qty,
                    desired_qty=ideal_qty_mid,
                    base_notional=base_notional,
                    eff_notional=base_notional,
                    redirected=0.0,
                    q=q,
                    ref_c=eff_ref,
                    lot=lot,
                    ibkr_s=ibkr_s,
                    exch=exch,
                    asset_cls=asset_cls,
                    note=f"Anti-panic {config.severe_sell_slices}-slice patient exit on {gap_pct:+.2%} gap.",
                )
            else:
                _emit_orders_and_decision(
                    sym=sym,
                    action=ActionCode.SELL_NORMAL,
                    cur_qty=cur_qty,
                    desired_qty=ideal_qty_mid,
                    base_notional=base_notional,
                    eff_notional=base_notional,
                    redirected=0.0,
                    q=q,
                    ref_c=eff_ref,
                    lot=lot,
                    ibkr_s=ibkr_s,
                    exch=exch,
                    asset_cls=asset_cls,
                    note=f"Normal sell/rebalance ({gap_pct:+.2%} vs Friday close).",
                )
            continue

        # Branch B: BUY / Hold
        inc_budget = max(0.0, base_notional - cur_qty * mid)
        if ideal_qty_mid == cur_qty or inc_budget <= 0.0:
            _emit_orders_and_decision(
                sym=sym,
                action=ActionCode.HOLD_AT_TARGET,
                cur_qty=cur_qty,
                desired_qty=cur_qty,
                base_notional=base_notional,
                eff_notional=base_notional,
                redirected=0.0,
                q=q,
                ref_c=eff_ref,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note="Position already at target lot count.",
            )
            continue

        if gap_pct < config.buy_adverse_gap_min:
            redirected_defensive += inc_budget
            alerts.append(
                f"[SKIP_ADVERSE_GAP_DOWN] {sym}: gap={gap_pct:+.2%} < {config.buy_adverse_gap_min:+.1%}; "
                f"falling-knife veto — routing {inc_budget:,.2f} {ccy} to {defensive_sym}."
            )
            _emit_orders_and_decision(
                sym=sym,
                action=ActionCode.SKIP_ADVERSE_GAP_DOWN,
                cur_qty=cur_qty,
                desired_qty=cur_qty,
                base_notional=base_notional,
                eff_notional=cur_qty * mid,
                redirected=inc_budget,
                q=q,
                ref_c=eff_ref,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note=f"Falling-knife news veto ({gap_pct:+.2%}); routed {inc_budget:,.2f} {ccy} -> {defensive_sym}.",
            )
        elif gap_pct > config.buy_moderate_gap_max:
            redirected_defensive += inc_budget
            alerts.append(
                f"[SKIP_GAP_UP] {sym}: surge gap={gap_pct:+.2%} > {config.buy_moderate_gap_max:+.1%}; "
                f"anti-chase circuit breaker — routing {inc_budget:,.2f} {ccy} to {defensive_sym}."
            )
            _emit_orders_and_decision(
                sym=sym,
                action=ActionCode.SKIP_GAP_UP,
                cur_qty=cur_qty,
                desired_qty=cur_qty,
                base_notional=base_notional,
                eff_notional=cur_qty * mid,
                redirected=inc_budget,
                q=q,
                ref_c=eff_ref,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note=f"Chase circuit breaker ({gap_pct:+.2%}); routed {inc_budget:,.2f} {ccy} -> {defensive_sym}.",
            )
        elif gap_pct > config.buy_mild_gap_max:
            keep_ratio = config.moderate_stage_ratio
            staged_notional = cur_qty * mid + keep_ratio * inc_budget
            redir = (1.0 - keep_ratio) * inc_budget
            redirected_defensive += redir
            staged_qty = floor_to_lot(staged_notional / mid, lot)
            alerts.append(
                f"[BUY_STAGED_50] {sym}: moderate bullish gap={gap_pct:+.2%}; "
                f"staging {keep_ratio:.0%} entry now and routing {redir:,.2f} {ccy} to {defensive_sym}."
            )
            _emit_orders_and_decision(
                sym=sym,
                action=ActionCode.BUY_STAGED_50,
                cur_qty=cur_qty,
                desired_qty=staged_qty,
                base_notional=base_notional,
                eff_notional=staged_notional,
                redirected=redir,
                q=q,
                ref_c=eff_ref,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note=f"50% staged patient entry ({gap_pct:+.2%}); {redir:,.2f} {ccy} -> {defensive_sym}.",
            )
        else:
            _emit_orders_and_decision(
                sym=sym,
                action=ActionCode.BUY_NORMAL,
                cur_qty=cur_qty,
                desired_qty=ideal_qty_mid,
                base_notional=base_notional,
                eff_notional=base_notional,
                redirected=0.0,
                q=q,
                ref_c=eff_ref,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note=f"Dynamic live-mid sizing ({gap_pct:+.2%} mild gap).",
            )

    # 2. Evaluate Defensive Fallback Sleeve LAST (absorbs all redirected_defensive notional)
    def_cur_qty = held_in_market[defensive_sym].quantity if defensive_sym in held_in_market else 0
    def_base = (
        float(mkt_targets[defensive_sym].portfolio_weight) * total_eq_local
        if defensive_sym in mkt_targets
        else 0.0
    )
    def_eff = def_base + redirected_defensive
    if def_eff > 0.0 or def_cur_qty > 0:
        ibkr_s, exch, asset_cls, lot, ref_c = _meta_for_symbol(defensive_sym)
        def_q = quotes.get(defensive_sym)
        if def_q is None or not def_q.is_valid:
            alerts.append(
                f"[HALTED_OR_NO_QUOTE] {defensive_sym}: defensive fallback quote unavailable; keeping cash."
            )
            _emit_orders_and_decision(
                sym=defensive_sym,
                action=ActionCode.HALTED_OR_NO_QUOTE,
                cur_qty=def_cur_qty,
                desired_qty=def_cur_qty,
                base_notional=def_base,
                eff_notional=def_eff,
                redirected=0.0,
                q=None,
                ref_c=ref_c,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note="Defensive fallback quote unavailable; keeping cash.",
            )
        else:
            def_desired = floor_to_lot(def_eff / def_q.mid, lot)
            def_act = (
                ActionCode.SELL_NORMAL
                if def_desired < def_cur_qty
                else (
                    ActionCode.BUY_DEFENSIVE_FALLBACK
                    if redirected_defensive > 0.0
                    else ActionCode.BUY_NORMAL
                )
            )
            note_txt = (
                f"Base {def_base:,.2f} + redirected {redirected_defensive:,.2f} = {def_eff:,.2f} {ccy}."
                if redirected_defensive > 0.0
                else f"Defensive sleeve target {def_base:,.2f} {ccy}."
            )
            _emit_orders_and_decision(
                sym=defensive_sym,
                action=def_act,
                cur_qty=def_cur_qty,
                desired_qty=def_desired,
                base_notional=def_base,
                eff_notional=def_eff,
                redirected=0.0,
                q=def_q,
                ref_c=ref_c if ref_c > 0 else def_q.mid,
                lot=lot,
                ibkr_s=ibkr_s,
                exch=exch,
                asset_cls=asset_cls,
                note=note_txt,
            )

    # Deterministic sort: Sells by symbol+slice, Buys with non-defensive first then defensive
    phase_a_sells.sort(key=lambda o: (o.symbol, o.slice_index))
    phase_b_buys.sort(key=lambda o: (o.symbol == defensive_sym, o.symbol, o.slice_index))
    decisions.sort(key=lambda d: d.symbol)

    return ExecutionBatchPlan(
        market=market,
        currency=ccy,
        execution_session=exec_session,
        total_equity_usd=total_eq_usd,
        total_equity_hkd=total_eq_hkd,
        usdhkd_rate=usdhkd_rate,
        redirected_defensive_notional=redirected_defensive,
        decisions=tuple(decisions),
        phase_a_sells=tuple(phase_a_sells),
        phase_b_buys=tuple(phase_b_buys),
        alerts=tuple(alerts),
    )


def _unit_cost_with_fee(spec: OrderSpec, config: ExecutionPolicyConfig) -> float:
    if spec.currency == "USD":
        fee_bps = config.us_fee_buffer_bps
    elif spec.asset_class == "etf":
        fee_bps = config.hk_etf_fee_buffer_bps
    else:
        fee_bps = config.hk_stock_fee_buffer_bps
    if (
        spec.side == "BUY"
        and spec.style != "PROTECTIVE_FLOOR_LIMIT"
        and config.max_walk_steps > 0
    ):
        worst_px = max(
            float(spec.limit_price),
            spec.arrival_mid * (1.0 + config.max_slippage_bps / 10_000.0),
        )
    else:
        worst_px = float(spec.limit_price)
    return worst_px * (1.0 + fee_bps / 10_000.0)


def _realized_sell_net_proceeds(
    sells: Sequence[OrderSpec],
    fills: Sequence[FillReport],
    config: ExecutionPolicyConfig,
) -> float:
    """Compute conservative fee-adjusted net proceeds from executed same-session Phase A sells."""
    spec_by_intent = {s.intent_id: s for s in sells}
    net = 0.0
    for fr in fills:
        if fr.filled_qty <= 0 or fr.avg_fill_price <= 0.0:
            continue
        spec = spec_by_intent.get(fr.intent_id)
        if spec is None or spec.side != "SELL":
            continue
        if spec.currency == "USD":
            fee_bps = config.us_fee_buffer_bps
        elif spec.asset_class == "etf":
            fee_bps = config.hk_etf_fee_buffer_bps
        else:
            fee_bps = config.hk_stock_fee_buffer_bps
        net += fr.filled_qty * fr.avg_fill_price * (1.0 - fee_bps / 10_000.0)
    return net


def compute_spendable_cash(
    cash_snap: CashSnap,
    *,
    reserve_amount: float = 0.0,
    config: ExecutionPolicyConfig = _DEFAULT_POLICY_CONFIG,
) -> float:
    """Compute non-borrowed spendable cash for Phase B buys (zero-margin-interest guard)."""
    if cash_snap.currency == "HKD" and config.require_settled_hkd:
        base = min(cash_snap.total_cash, cash_snap.settled_cash, cash_snap.available_funds)
    else:
        base = min(cash_snap.total_cash, cash_snap.available_funds)
    return max(0.0, base - max(0.0, reserve_amount))


def apply_cash_cap_to_buys(
    buy_orders: Sequence[OrderSpec],
    *,
    spendable_cash: float,
    config: ExecutionPolicyConfig = _DEFAULT_POLICY_CONFIG,
) -> tuple[tuple[OrderSpec, ...], list[str]]:
    """Ensure total Phase B buy outlay + fees never exceeds `spendable_cash`."""
    if not buy_orders:
        return (), []
    ccy = buy_orders[0].currency
    min_notional = config.min_trade_hkd if ccy == "HKD" else config.min_trade_usd

    if spendable_cash <= 0.0:
        return (), [
            f"[ZERO_MARGIN_CASH_GUARD] Spendable non-borrowed {ccy} cash is 0.00; "
            f"blocked {len(buy_orders)} {ccy} buy order(s) to prevent margin borrowing."
        ]

    total_required = sum(o.quantity * _unit_cost_with_fee(o, config) for o in buy_orders)
    if total_required <= spendable_cash:
        return tuple(buy_orders), []

    scale = spendable_cash / total_required
    warnings = [
        f"[ZERO_MARGIN_CASH_GUARD] Scaling {ccy} buys by {scale:.4f} so outlay fits "
        f"spendable non-borrowed {ccy} cash ({spendable_cash:,.2f} < {total_required:,.2f})."
    ]
    capped: list[OrderSpec] = []
    remaining = spendable_cash
    for o in buy_orders:
        uc = _unit_cost_with_fee(o, config)
        max_affordable = remaining / uc if uc > 0 else 0.0
        scaled_qty = floor_to_lot(min(o.quantity * scale, max_affordable), o.lot_size)
        if scaled_qty <= 0 or (scaled_qty * float(o.limit_price)) < min_notional:
            continue
        cost = scaled_qty * uc
        if cost <= remaining + 1e-6:
            capped_spec = (
                replace(
                    o,
                    intent_id=f"{o.intent_id}:cap:{o.quantity}:{scaled_qty}",
                    quantity=scaled_qty,
                )
                if scaled_qty < o.quantity
                else o
            )
            capped.append(capped_spec)
            remaining = max(0.0, remaining - cost)

    return tuple(capped), warnings


def _execute_orders_with_walk(
    broker: BrokerAdapter,
    orders: Sequence[OrderSpec],
    *,
    dry_run: bool,
    config: ExecutionPolicyConfig,
    sleep_fn: Callable[[float], None] | None = None,
) -> list[FillReport]:
    reports: list[FillReport] = []
    for spec in orders:
        rep = broker.place_order(spec, dry_run=dry_run)
        if dry_run or rep.status in ("FILLED", "CANCELLED", "DRY_RUN"):
            reports.append(rep)
            continue

        cur_spec = spec
        for step in range(1, config.max_walk_steps + 1):
            if sleep_fn is not None:
                if config.poll_interval_sec > 0:
                    sleep_fn(config.poll_interval_sec)
                rep = broker.poll_order(rep.order_id, wait_sec=0.0)
            else:
                rep = broker.poll_order(rep.order_id, wait_sec=config.poll_interval_sec)
            if rep.status in ("FILLED", "CANCELLED"):
                break
            q_map = broker.get_quotes([cur_spec.symbol])
            rep = broker.poll_order(rep.order_id, wait_sec=0.0)
            if rep.status in ("FILLED", "CANCELLED"):
                break
            live_q = q_map.get(cur_spec.symbol)
            new_lmt = compute_walked_limit_price(cur_spec, live_q, step, config)
            if new_lmt is None:
                if any(
                    compute_walked_limit_price(cur_spec, live_q, later_step, config) is not None
                    for later_step in range(step + 1, config.max_walk_steps + 1)
                ):
                    continue
                break
            rep = broker.modify_order_limit(rep.order_id, cur_spec, new_lmt)
            if rep.status in ("FILLED", "CANCELLED"):
                break
            cur_spec = replace(cur_spec, limit_price=rep.final_limit_price)
        reports.append(rep)
    return reports


def execute_market_rebalance(
    plan: WeeklyPlan,
    broker: BrokerAdapter,
    *,
    market: MarketCode,
    dry_run: bool = True,
    use_adaptive: bool = True,
    bootstrap_fx_usd: float = 0.0,
    allow_fx_convert: bool = False,
    state_dir: Path | None = None,
    config: ExecutionPolicyConfig = _DEFAULT_POLICY_CONFIG,
    sleep_fn: Callable[[float], None] | None = None,
) -> ExecutionReport:
    """Run two-phase Sell-then-Buy execution with walk-limit repricing and state persistence."""
    exec_session = (
        plan.hk_execution_session.isoformat()
        if market == "HK"
        else plan.us_execution_session.isoformat()
    )
    state_path = (
        state_dir / f"execution_state_{exec_session.replace('-', '')}_{market}.json"
        if state_dir is not None
        else None
    )
    persisted = (
        load_execution_state(state_path, expected_plan_sha256=plan.plan_sha256)
        if state_path is not None
        else None
    )
    completed: dict[str, dict[str, Any]] = dict(persisted.completed_intents) if persisted else {}

    positions = broker.get_positions()
    cash = broker.get_cash()
    open_orders = broker.get_open_orders()
    usdhkd = broker.get_usdhkd_rate(default=config.default_usdhkd_rate)

    ccy_market: CurrencyCode = "HKD" if market == "HK" else "USD"
    defensive_sym = HK_DEFENSIVE_SYMBOL if market == "HK" else US_DEFENSIVE_SYMBOL
    needed_quote_syms = sorted(
        {
            *(t.symbol for t in plan.targets_for_market(market)),
            *(
                sym
                for sym, pos in positions.items()
                if pos.currency == ccy_market and pos.quantity != 0
            ),
            defensive_sym,
        }
    )
    quotes = broker.get_quotes(needed_quote_syms)

    batch = build_execution_batch(
        plan,
        market=market,
        positions=positions,
        cash=cash,
        open_orders=open_orders,
        quotes=quotes,
        usdhkd_rate=usdhkd,
        use_adaptive=use_adaptive,
        persisted_state=persisted,
        config=config,
    )
    alerts = list(batch.alerts)

    # Phase A: Execute ALL Sells FIRST (including SGOV.US on initial bootstrap!)
    phase_a_fills = _execute_orders_with_walk(
        broker,
        batch.phase_a_sells,
        dry_run=dry_run,
        config=config,
        sleep_fn=sleep_fn,
    )
    if not dry_run:
        for fr in phase_a_fills:
            if fr.status == "FILLED":
                completed[fr.intent_id] = fr.to_dict()
        if state_path is not None:
            save_execution_state(
                ExecutionState(
                    plan_sha256=plan.plan_sha256,
                    execution_session=exec_session,
                    market=market,
                    completed_intents=completed,
                    updated_at_utc=datetime.now(UTC).isoformat(),
                ),
                state_path,
            )

    # Refresh cash snapshot after Phase A sells before optional FX & Phase B cash cap
    ccy: CurrencyCode = "HKD" if market == "HK" else "USD"
    after_sells_c = broker.get_cash().get(ccy, CashSnap(ccy, 0.0, 0.0, 0.0))

    # Optional FX conversion during US bootstrap
    fx_report: FxConversionReport | None = None
    fx_executed_this_run = False
    reserve_usd = 0.0
    if market == "US" and bootstrap_fx_usd > 0.0:
        fx_intent_id = _make_intent_id(plan.plan_sha256, exec_session, "US", "USD.HKD", "SELL", 1)
        fx_done_in_state = fx_intent_id in completed
        fx_open_working = any(
            oo.symbol == "USD.HKD" and oo.side == "SELL" and oo.remaining_qty > 0
            for oo in open_orders
        )
        if dry_run:
            if allow_fx_convert:
                fx_report = broker.convert_usd_to_hkd(bootstrap_fx_usd, dry_run=True)
                alerts.append(fx_report.settlement_warning)
            else:
                reserve_usd = bootstrap_fx_usd
                alerts.append(
                    f"[FX_RESERVE_NOTE] Reserved ${bootstrap_fx_usd:,.2f} USD unspent for HK sleeve; "
                    f"pass --allow-fx-convert (after T+1 US settlement for zero margin interest) to convert USD.HKD."
                )
        elif fx_done_in_state or fx_open_working:
            reserve_usd = bootstrap_fx_usd if fx_open_working else 0.0
            if allow_fx_convert:
                prev_fx = completed.get(fx_intent_id, {})
                fx_report = FxConversionReport(
                    pair="USD.HKD",
                    sell_currency="USD",
                    buy_currency="HKD",
                    sell_amount=float(prev_fx.get("sell_amount", round(bootstrap_fx_usd))),
                    estimated_buy_amount=float(
                        prev_fx.get(
                            "estimated_buy_amount", round(bootstrap_fx_usd * usdhkd, 2)
                        )
                    ),
                    rate=float(prev_fx.get("rate", usdhkd)),
                    order_id=str(prev_fx.get("order_id", "OPEN-FX-USDHKD")),
                    status="ALREADY_COMPLETED",
                    settlement_warning=(
                        "FX conversion already completed or working for this plan session; "
                        "skipping duplicate USD.HKD conversion."
                    ),
                )
                alerts.append(fx_report.settlement_warning)
        elif allow_fx_convert:
            pre_fx_c = cash.get("USD", CashSnap("USD", 0.0, 0.0, 0.0))
            realized_sell_net_fx = _realized_sell_net_proceeds(
                batch.phase_a_sells, phase_a_fills, config
            )
            pre_fx_total = max(after_sells_c.total_cash, pre_fx_c.total_cash + realized_sell_net_fx)
            pre_fx_avail = max(
                after_sells_c.available_funds, pre_fx_c.available_funds + realized_sell_net_fx
            )
            non_borrowed_usd = min(pre_fx_total, pre_fx_avail)
            if non_borrowed_usd + 1e-6 < bootstrap_fx_usd:
                reserve_usd = bootstrap_fx_usd
                alerts.append(
                    f"[ZERO_MARGIN_FX_GUARD] Non-borrowed USD cash ({non_borrowed_usd:,.2f}) < "
                    f"requested FX conversion ({bootstrap_fx_usd:,.2f}); skipped USD.HKD "
                    f"conversion to prevent margin borrowing."
                )
            else:
                fx_report = broker.convert_usd_to_hkd(bootstrap_fx_usd, dry_run=False)
                alerts.append(fx_report.settlement_warning)
                if fx_report.status.upper() in ("INACTIVE", "CANCELLED", "APICANCELLED"):
                    reserve_usd = bootstrap_fx_usd
                else:
                    fx_executed_this_run = True
                    completed[fx_intent_id] = {
                        "order_id": fx_report.order_id,
                        "intent_id": fx_intent_id,
                        "symbol": "USD.HKD",
                        "side": "SELL",
                        "sell_amount": fx_report.sell_amount,
                        "estimated_buy_amount": fx_report.estimated_buy_amount,
                        "rate": fx_report.rate,
                        "status": fx_report.status,
                    }
                    if state_path is not None:
                        save_execution_state(
                            ExecutionState(
                                plan_sha256=plan.plan_sha256,
                                execution_session=exec_session,
                                market=market,
                                completed_intents=completed,
                                updated_at_utc=datetime.now(UTC).isoformat(),
                            ),
                            state_path,
                        )
        else:
            reserve_usd = bootstrap_fx_usd
            alerts.append(
                f"[FX_RESERVE_NOTE] Reserved ${bootstrap_fx_usd:,.2f} USD unspent for HK sleeve; "
                f"pass --allow-fx-convert (after T+1 US settlement for zero margin interest) to convert USD.HKD."
            )

    if dry_run:
        # Simulate same-session Phase A sell proceeds (US T+1 -> US T+1, SEHK T+2 -> SEHK T+2)
        est_proceeds = sum(
            s.quantity * float(s.limit_price) * (0.99985 if ccy == "USD" else 0.9988)
            for s in batch.phase_a_sells
            if s.style != "PROTECTIVE_FLOOR_LIMIT"
        )
        fx_dry_deduct = (
            fx_report.sell_amount if (ccy == "USD" and fx_report is not None) else 0.0
        )
        sim_cash = CashSnap(
            currency=ccy,
            total_cash=max(0.0, after_sells_c.total_cash + est_proceeds - fx_dry_deduct),
            settled_cash=max(0.0, after_sells_c.settled_cash + est_proceeds - fx_dry_deduct),
            available_funds=max(0.0, after_sells_c.available_funds + est_proceeds - fx_dry_deduct),
        )
        spendable = compute_spendable_cash(sim_cash, reserve_amount=reserve_usd, config=config)
    else:
        # Credit realized same-session Phase A sells (same settlement cycle as Phase B buys:
        # US T+1 == T+1, SEHK CCASS T+2 == T+2) while keeping pre-existing unsettled FX isolated!
        pre_c = cash.get(ccy, CashSnap(ccy, 0.0, 0.0, 0.0))
        realized_sell_net = _realized_sell_net_proceeds(batch.phase_a_sells, phase_a_fills, config)
        eff_total = max(after_sells_c.total_cash, pre_c.total_cash + realized_sell_net)
        eff_settled = min(
            eff_total,
            max(after_sells_c.settled_cash, pre_c.settled_cash + realized_sell_net),
        )
        eff_avail = max(after_sells_c.available_funds, pre_c.available_funds + realized_sell_net)
        if ccy == "USD" and fx_executed_this_run and fx_report is not None:
            post_fx_c = broker.get_cash().get("USD", after_sells_c)
            eff_total = max(0.0, min(post_fx_c.total_cash, eff_total - fx_report.sell_amount))
            eff_settled = max(
                0.0, min(eff_total, post_fx_c.settled_cash, eff_settled - fx_report.sell_amount)
            )
            eff_avail = max(
                0.0, min(post_fx_c.available_funds, eff_avail - fx_report.sell_amount)
            )
        eff_cash = CashSnap(
            currency=ccy,
            total_cash=eff_total,
            settled_cash=eff_settled,
            available_funds=eff_avail,
        )
        spendable = compute_spendable_cash(eff_cash, reserve_amount=reserve_usd, config=config)

    capped_buys, cap_warnings = apply_cash_cap_to_buys(
        batch.phase_b_buys, spendable_cash=spendable, config=config
    )
    if not dry_run and completed:
        capped_buys = tuple(o for o in capped_buys if o.intent_id not in completed)
    alerts.extend(cap_warnings)

    # Phase B: Execute Capped Buys
    phase_b_fills = _execute_orders_with_walk(
        broker,
        capped_buys,
        dry_run=dry_run,
        config=config,
        sleep_fn=sleep_fn,
    )
    if not dry_run:
        for fr in phase_b_fills:
            if fr.status == "FILLED":
                completed[fr.intent_id] = fr.to_dict()
        if state_path is not None:
            save_execution_state(
                ExecutionState(
                    plan_sha256=plan.plan_sha256,
                    execution_session=exec_session,
                    market=market,
                    completed_intents=completed,
                    updated_at_utc=datetime.now(UTC).isoformat(),
                ),
                state_path,
            )

    return ExecutionReport(
        batch=batch,
        phase_a_fills=tuple(phase_a_fills),
        fx_report=fx_report,
        capped_phase_b_buys=capped_buys,
        phase_b_fills=tuple(phase_b_fills),
        spendable_cash_before_buys=spendable,
        alerts=tuple(alerts),
        state_path=state_path,
    )
