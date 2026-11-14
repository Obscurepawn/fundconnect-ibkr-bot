"""Standalone implementation of Strategy 3: `hk_etf_southbound_rot_v1`."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from ..config import (
    HK_BENCHMARK_ETF,
    HK_EQUITY_ETFS,
    HK_ETF_UNIVERSE,
    HK_GOLD_ETF,
    HK_MONEY_ETF,
    HK_TZ,
)
from ..data.calendars import hk_close_time
from ..data.indexone import HkBarRecord

_QUANT = Decimal("0.00000001")


def quantize_hk_weights(raw_weights: Mapping[str, float], max_gross: Decimal) -> dict[str, Decimal]:
    """Convert nonnegative float weights into exact Fixed8 Decimals summing to `<= max_gross`."""
    positive = {k: v for k, v in sorted(raw_weights.items()) if math.isfinite(v) and v > 1e-6}
    if not positive:
        return {}
    total = sum(positive.values())
    cap_float = float(max_gross)
    scale = min(1.0, cap_float / total) if total > cap_float else 1.0
    result: dict[str, Decimal] = {}
    running = Decimal(0)
    for sym, w in positive.items():
        dec = Decimal(str(w * scale)).quantize(_QUANT)
        if running + dec > max_gross:
            dec = max(Decimal(0), (max_gross - running).quantize(_QUANT))
        if dec > 0:
            result[sym] = dec
            running += dec
    return result


def _parse_ymd(ymd: str) -> date:
    return date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))


def _rolling_zscore(series: Sequence[float], window: int = 60) -> list[float | None]:
    out: list[float | None] = []
    for idx in range(len(series)):
        if idx + 1 < window:
            out.append(None)
            continue
        seg = series[idx + 1 - window : idx + 1]
        sd = statistics.stdev(seg)
        if sd <= 1e-12:
            out.append(0.0)
        else:
            z = (seg[-1] - statistics.fmean(seg)) / sd
            out.append(max(-3.5, min(3.5, z)))
    return out


def compute_southbound_macro_at_session(
    asof_date: date,
    all_sessions: Sequence[date],
    next_session: date,
    hk_hold_rows: Sequence[Mapping[str, Any]],
    ggt_rows: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None]:
    """Compute `(sb_trend_z, sb_composite_z60)` stamped strictly T+1 08:30 HKT (`<= asof close`)."""
    extended_sessions = (*all_sessions, next_session)
    by_date: dict[str, dict[str, int]] = defaultdict(dict)
    for row in hk_hold_rows:
        by_date[str(row["trade_date"])][str(row["ts_code"])] = int(row["vol"])

    ggt_by_date: dict[str, tuple[float, float]] = {}
    for row in ggt_rows:
        ggt_by_date[str(row["trade_date"])] = (
            float(row["buy_amount"]),
            float(row["sell_amount"]),
        )

    hold_dates = sorted(by_date)
    breadth_dates: list[date] = []
    breadth_vals: list[float] = []
    breadth20_vals: list[float] = []
    net_flow_vals: list[float] = []
    net_flow20_vals: list[float] = []
    ggt_sorted = sorted(ggt_by_date)

    for idx, td_str in enumerate(hold_dates):
        if idx < 20:
            continue
        cur = by_date[td_str]
        prev5 = by_date[hold_dates[idx - 5]]
        prev20 = by_date[hold_dates[idx - 20]]
        common5 = [tc for tc in cur if tc in prev5 and prev5[tc] > 0]
        common20 = [tc for tc in cur if tc in prev20 and prev20[tc] > 0]
        if len(common5) < 200 or len(common20) < 200:
            continue
        adv5 = sum(1 for tc in common5 if cur[tc] > prev5[tc]) / len(common5)
        adv20 = sum(1 for tc in common20 if cur[tc] > prev20[tc]) / len(common20)

        g_idx = bisect_right(ggt_sorted, td_str)
        if g_idx >= 5:
            g_slice5 = [ggt_by_date[ggt_sorted[j]] for j in range(g_idx - 5, g_idx)]
            b5 = sum(x[0] for x in g_slice5)
            s5 = sum(x[1] for x in g_slice5)
            net_ratio5 = (b5 - s5) / max(b5 + s5, 1e-6)
        else:
            net_ratio5 = 0.0
        if g_idx >= 20:
            g_slice20 = [ggt_by_date[ggt_sorted[j]] for j in range(g_idx - 20, g_idx)]
            b20 = sum(x[0] for x in g_slice20)
            s20 = sum(x[1] for x in g_slice20)
            net_ratio20 = (b20 - s20) / max(b20 + s20, 1e-6)
        else:
            net_ratio20 = net_ratio5

        breadth_dates.append(_parse_ymd(td_str))
        breadth_vals.append(adv5)
        breadth20_vals.append(adv20)
        net_flow_vals.append(net_ratio5)
        net_flow20_vals.append(net_ratio20)

    b_z60 = _rolling_zscore(breadth_vals, 60)
    f_z60 = _rolling_zscore(net_flow_vals, 60)
    b20_z100 = _rolling_zscore(breadth20_vals, 100)
    f20_z100 = _rolling_zscore(net_flow20_vals, 100)

    cutoff_hkt = hk_close_time(asof_date)
    by_avail: dict[datetime, tuple[float, float]] = {}
    for obs_d, bz, fz, b20z, f20z in zip(
        breadth_dates, b_z60, f_z60, b20_z100, f20_z100, strict=True
    ):
        if bz is None or fz is None or b20z is None or f20z is None:
            continue
        pos = bisect_right(extended_sessions, obs_d)
        if pos >= len(extended_sessions):
            continue
        avail_session = extended_sessions[pos]
        avail_dt = datetime.combine(avail_session, time(8, 30), tzinfo=HK_TZ)
        if avail_dt <= cutoff_hkt:
            comp_z = 0.60 * bz + 0.40 * fz
            trend_z = 0.60 * b20z + 0.40 * f20z
            by_avail[avail_dt] = (trend_z, comp_z)

    if not by_avail:
        return None, None
    return by_avail[max(by_avail)]


def compute_hk_etf_targets(
    asof_date: date,
    all_sessions: Sequence[date],
    next_session: date,
    bars_by_sym: Mapping[str, Mapping[date, HkBarRecord]],
    hk_hold_rows: Sequence[Mapping[str, Any]],
    ggt_rows: Sequence[Mapping[str, Any]],
    *,
    actual_sleeve_weights: Mapping[str, float] | None = None,
    max_gross: Decimal = Decimal("0.94000000"),
    target_count: int = 2,
    hold_buffer_rank: int = 3,
    drift_threshold: float = 0.075,
) -> tuple[dict[str, Decimal], dict[str, Any]]:
    """Compute `hk_etf_southbound_rot_v1` sleeve weights as of `asof_date` close."""
    sess_up_to = [d for d in all_sessions if d <= asof_date]
    if not sess_up_to or sess_up_to[-1] != asof_date or len(sess_up_to) < 121:
        raise ValueError(f"Insufficient HKEX session history up to {asof_date}")

    sb_trend_z, sb_z60 = compute_southbound_macro_at_session(
        asof_date, sess_up_to, next_session, hk_hold_rows, ggt_rows
    )
    if sb_trend_z is None or not math.isfinite(sb_trend_z):
        raise ValueError(f"Missing Southbound macro trend Z-score on {asof_date}")

    r5: dict[str, float] = {}
    r20: dict[str, float] = {}
    r60: dict[str, float] = {}
    r120: dict[str, float] = {}
    sma60: dict[str, float] = {}
    vol10: dict[str, float] = {}
    vol20: dict[str, float] = {}
    vol60: dict[str, float] = {}

    for sym in HK_ETF_UNIVERSE:
        closes = [float(bars_by_sym[sym][d].adj_close) for d in sess_up_to]
        rets = [0.0] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        r5[sym] = closes[-1] / closes[-6] - 1.0
        r20[sym] = closes[-1] / closes[-21] - 1.0
        r60[sym] = closes[-1] / closes[-61] - 1.0
        r120[sym] = closes[-1] / closes[-121] - 1.0
        sma60[sym] = closes[-1] / statistics.fmean(closes[-60:]) - 1.0
        vol10[sym] = statistics.stdev(rets[-10:]) * math.sqrt(252.0)
        vol20[sym] = statistics.stdev(rets[-20:]) * math.sqrt(252.0)
        vol60[sym] = statistics.stdev(rets[-60:]) * math.sqrt(252.0)

    hsi_v10 = vol10[HK_BENCHMARK_ETF]
    hsi_v20 = vol20[HK_BENCHMARK_ETF]
    hsi_v60 = vol60[HK_BENCHMARK_ETF]
    hsi_r5 = r5[HK_BENCHMARK_ETF]
    hsi_sma60 = sma60[HK_BENCHMARK_ETF]

    vol_spike_ratio = hsi_v10 / max(hsi_v60, 0.10)
    acute_shock = vol_spike_ratio > 1.50 and hsi_r5 < -0.045
    sb_bear_gate = hsi_sma60 < -0.025 and sb_trend_z < -0.35

    directional_syms = (*HK_EQUITY_ETFS, HK_GOLD_ETF)
    scores: dict[str, float] = {}
    moms: dict[str, float] = {}
    smas: dict[str, float] = {}
    vols: dict[str, float] = {}

    for sym in directional_syms:
        if sym in HK_EQUITY_ETFS and (acute_shock or sb_bear_gate):
            continue
        m20, m60, m120 = r20[sym], r60[sym], r120[sym]
        s60, v20_val = sma60[sym], vol20[sym]
        mom_blend = 0.35 * m20 + 0.40 * m60 + 0.25 * m120
        eff_vol = max(v20_val, 0.11)
        base_score = mom_blend / eff_vol
        if sym in HK_EQUITY_ETFS:
            base_score += 0.25 * sb_trend_z
        scores[sym] = base_score
        moms[sym] = mom_blend
        smas[sym] = s60
        vols[sym] = eff_vol

    ranked = sorted(scores, key=lambda s: (-scores[s], s))
    rank_of = {sym: idx + 1 for idx, sym in enumerate(ranked)}

    actual = dict(actual_sleeve_weights or {})
    held_now = {s for s in directional_syms if actual.get(s, 0.0) > 0.05}

    selected: list[str] = []
    for sym in sorted(held_now, key=lambda s: (rank_of.get(s, 999), s)):
        if (
            sym in rank_of
            and rank_of[sym] <= hold_buffer_rank
            and moms[sym] > -0.01
            and smas[sym] > -0.02
            and len(selected) < target_count
        ):
            selected.append(sym)

    for sym in ranked:
        if len(selected) >= target_count:
            break
        if sym in selected:
            continue
        if moms[sym] > 0.0 and smas[sym] > -0.01 and scores[sym] > 0.05:
            selected.append(sym)

    cap_f = float(max_gross)
    raw_weights: dict[str, float] = {}
    if len(selected) >= 2:
        eq_vol_scale = (
            min(1.0, 0.26 / max(hsi_v20, 0.12))
            if any(s in HK_EQUITY_ETFS for s in selected)
            else 1.0
        )
        dir_budget = cap_f * eq_vol_scale
        inv_vols = {s: 1.0 / vols[s] for s in selected}
        inv_sum = sum(inv_vols.values())
        for s in selected:
            raw_weights[s] = min(0.58, dir_budget * (inv_vols[s] / inv_sum))
        rem = max(0.0, cap_f - sum(raw_weights.values()))
        if rem >= 0.12:
            raw_weights[HK_MONEY_ETF] = rem - 0.02
    elif len(selected) == 1:
        only_sym = selected[0]
        single_w = min(0.62, cap_f * min(1.0, 0.22 / vols[only_sym]))
        raw_weights[only_sym] = single_w
        rem = max(0.0, cap_f - single_w)
        if rem >= 0.10:
            raw_weights[HK_MONEY_ETF] = rem - 0.02
    else:
        raw_weights[HK_MONEY_ETF] = cap_f - 0.02

    proposed = quantize_hk_weights(raw_weights, max_gross)
    retained_drift = False
    if actual:
        all_keys = set(proposed) | set(actual)
        max_diff = max(
            (abs(float(proposed.get(k, Decimal(0))) - actual.get(k, 0.0)) for k in all_keys),
            default=0.0,
        )
        if set(proposed) == set(actual) and max_diff < drift_threshold:
            retained_drift = True
            proposed = quantize_hk_weights(actual, max_gross)

    diagnostics: dict[str, Any] = {
        "session": asof_date.isoformat(),
        "sb_trend_z": round(sb_trend_z, 4),
        "sb_z60": round(sb_z60 or 0.0, 4),
        "acute_shock": acute_shock,
        "selected": selected,
        "retained_via_drift_band": retained_drift,
    }
    return proposed, diagnostics
