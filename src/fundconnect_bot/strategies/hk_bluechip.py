"""Standalone implementation of Strategy 4: `hk_southbound_bluechip_v1`."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from ..config import HK_BENCHMARK_ETF, HK_BLUECHIP_UNIVERSE, HK_TZ
from ..data.calendars import hk_close_time
from ..data.indexone import HkBarRecord
from ..data.tushare import indexone_to_tushare_hk
from .hk_etf import compute_southbound_macro_at_session, quantize_hk_weights


def _parse_ymd(ymd: str) -> date:
    return date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))


def _cross_sectional_zscore(values: Mapping[str, float]) -> dict[str, float]:
    if len(values) < 3:
        return dict.fromkeys(values, 0.0)
    arr = list(values.values())
    mean = statistics.fmean(arr)
    sd = statistics.stdev(arr)
    if sd <= 1e-12:
        return dict.fromkeys(values, 0.0)
    return {k: max(-3.0, min(3.0, (v - mean) / sd)) for k, v in values.items()}


def _compute_residual_momentum_last(
    stock_rets: Sequence[float], mkt_rets: Sequence[float], lookback: int, skip: int
) -> float | None:
    """Blitz, Huij & Martens (2011) residual momentum on the final bar (`t = len - 1`)."""
    t = len(stock_rets) - 1
    if t < lookback:
        return None
    s_win = stock_rets[t - lookback + 1 : t - skip + 1]
    m_win = mkt_rets[t - lookback + 1 : t - skip + 1]
    mean_s = statistics.fmean(s_win)
    mean_m = statistics.fmean(m_win)
    var_m = sum((m - mean_m) ** 2 for m in m_win)
    if var_m <= 1e-14:
        beta = 0.0
    else:
        cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(s_win, m_win, strict=True))
        beta = cov / var_m
    alpha_hat = mean_s - beta * mean_m
    resids = [s - beta * m for s, m in zip(s_win, m_win, strict=True)]
    denom = math.sqrt(sum(r * r for r in resids))
    if denom <= 1e-12 or not math.isfinite(alpha_hat):
        return 0.0
    return sum(resids) / denom


def compute_hk_bluechip_targets(
    asof_date: date,
    all_sessions: Sequence[date],
    next_session: date,
    bars_by_sym: Mapping[str, Mapping[date, HkBarRecord]],
    hk_hold_rows: Sequence[Mapping[str, Any]],
    ggt_rows: Sequence[Mapping[str, Any]],
    *,
    actual_sleeve_weights: Mapping[str, float] | None = None,
    prev_regime_scale: float | None = None,
    max_gross: Decimal = Decimal("0.93000000"),
    target_count: int = 7,
    hold_buffer_rank: int = 11,
    max_single_weight: float = 0.165,
    drift_threshold: float = 0.055,
) -> tuple[dict[str, Decimal], dict[str, Any]]:
    """Compute `hk_southbound_bluechip_v1` sleeve weights as of `asof_date` close.

    When `actual_sleeve_weights` is empty or `None` (cold-start HK sleeve), performs a pure
    Top-7 cross-sectional evaluation. When live IBKR HK blue-chip holdings are passed in on
    subsequent weeks, seamlessly applies Top-11 rank-buffer hysteresis, retained-position
    weight freezing (`<= 3.5%` band), and `5.5%` drift gating.
    """
    sess_up_to = [d for d in all_sessions if d <= asof_date]
    if not sess_up_to or sess_up_to[-1] != asof_date or len(sess_up_to) < 121:
        raise ValueError(f"Insufficient HKEX session history up to {asof_date}")

    extended_sessions = (*sess_up_to, next_session)
    cutoff_hkt = hk_close_time(asof_date)

    sb_trend_z_opt, _ = compute_southbound_macro_at_session(
        asof_date, sess_up_to, next_session, hk_hold_rows, ggt_rows
    )
    sb_trend_z = sb_trend_z_opt or 0.0

    mkt_closes = [float(bars_by_sym[HK_BENCHMARK_ETF][d].adj_close) for d in sess_up_to]
    mkt_rets = [0.0] + [mkt_closes[i] / mkt_closes[i - 1] - 1.0 for i in range(1, len(mkt_closes))]
    mkt_sma60 = mkt_closes[-1] / statistics.fmean(mkt_closes[-60:]) - 1.0
    mkt_sma120 = mkt_closes[-1] / statistics.fmean(mkt_closes[-120:]) - 1.0
    mkt_v10 = statistics.stdev(mkt_rets[-10:]) * math.sqrt(252.0)
    mkt_v20 = statistics.stdev(mkt_rets[-20:]) * math.sqrt(252.0)
    mkt_v60 = statistics.stdev(mkt_rets[-60:]) * math.sqrt(252.0)

    vol_scale = min(1.0, 0.25 / max(mkt_v20, 0.12))
    if (mkt_sma120 < -0.04 and sb_trend_z < -0.40) or (
        mkt_v10 > 1.45 * max(mkt_v60, 0.10) and mkt_sma60 < -0.03
    ):
        regime_scale = 0.50
    elif mkt_sma60 < -0.03 and sb_trend_z < -0.35:
        regime_scale = 0.75
    else:
        regime_scale = 1.0

    eff_prev_regime = 1.0 if prev_regime_scale is None else prev_regime_scale
    regime_changed = abs(regime_scale - eff_prev_regime) >= 0.20

    # Index CCASS rows by canonical 4-digit HK symbol
    ts_to_sym = {indexone_to_tushare_hk(s): s for s in HK_BLUECHIP_UNIVERSE}
    hold_by_sym: dict[str, dict[date, tuple[int, float]]] = defaultdict(dict)
    for row in hk_hold_rows:
        tc = str(row["ts_code"])
        if tc not in ts_to_sym:
            continue
        obs_d = _parse_ymd(str(row["trade_date"]))
        hold_by_sym[ts_to_sym[tc]][obs_d] = (int(row["vol"]), float(row["ratio"]))

    abn5_valid: dict[str, float] = {}
    abn20_valid: dict[str, float] = {}
    persist_valid: dict[str, float] = {}
    mom_valid: dict[str, float] = {}
    vols_valid: dict[str, float] = {}
    sma_valid: dict[str, float] = {}
    crowd_valid: dict[str, float] = {}

    for sym in HK_BLUECHIP_UNIVERSE:
        sym_bars = bars_by_sym.get(sym)
        if not sym_bars or asof_date not in sym_bars:
            continue
        closes = [float(sym_bars[d].adj_close) for d in sess_up_to]
        rets = [0.0] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        r20_val = _compute_residual_momentum_last(rets, mkt_rets, lookback=20, skip=3)
        r60_val = _compute_residual_momentum_last(rets, mkt_rets, lookback=60, skip=3)
        if r20_val is None or r60_val is None:
            continue
        mm = 0.50 * r20_val + 0.50 * r60_val
        s60 = closes[-1] / statistics.fmean(closes[-60:]) - 1.0
        v20 = statistics.stdev(rets[-20:]) * math.sqrt(252.0)

        obs_map = hold_by_sym.get(sym, {})
        if not obs_map:
            continue
        obs_dates_sorted = sorted(obs_map)
        aligned_sessions: list[date] = []
        split_adj_vols: list[float] = []
        ratios: list[float] = []
        split_prices: list[float] = []
        hkd_turnovers: list[float] = []

        for d in sess_up_to:
            pos = bisect_right(obs_dates_sorted, d) - 1
            if pos < 0:
                continue
            obs_d = obs_dates_sorted[pos]
            if (d - obs_d).days > 15:
                continue
            vol_raw, ratio_pct = obs_map[obs_d]
            bar = sym_bars[d]
            split_factor = bar.raw_close / bar.split_adj_close
            aligned_sessions.append(d)
            split_adj_vols.append(float(vol_raw) * split_factor)
            ratios.append(ratio_pct)
            split_prices.append(bar.split_adj_close)
            hkd_turnovers.append(bar.raw_close * bar.volume)

        per_avail: dict[datetime, tuple[float, float, float, float]] = {}
        for idx, d in enumerate(aligned_sessions):
            if idx < 20:
                continue
            next_pos = bisect_right(extended_sessions, d)
            if next_pos >= len(extended_sessions):
                continue
            avail_session = extended_sessions[next_pos]
            avail_dt = datetime.combine(avail_session, time(8, 30), tzinfo=HK_TZ)
            if avail_dt > cutoff_hkt:
                continue
            turn5 = sum(hkd_turnovers[idx - 4 : idx + 1]) + 1.0
            turn20 = sum(hkd_turnovers[idx - 19 : idx + 1]) + 1.0
            abn5 = ((split_adj_vols[idx] - split_adj_vols[idx - 5]) * split_prices[idx - 5]) / turn5
            abn20 = (
                (split_adj_vols[idx] - split_adj_vols[idx - 20]) * split_prices[idx - 20]
            ) / turn20
            persist20 = (
                sum(
                    1
                    for j in range(idx - 19, idx + 1)
                    if ratios[j] > ratios[j - 1] or split_adj_vols[j] > split_adj_vols[j - 1]
                )
                / 20.0
            )
            hist_start = max(0, idx - 251)
            ratio_hist = sorted(ratios[hist_start : idx + 1])
            p85 = ratio_hist[min(len(ratio_hist) - 1, int(0.85 * len(ratio_hist)))]
            rel_jump_5d = (ratios[idx] / ratios[idx - 5] - 1.0) if ratios[idx - 5] > 0.1 else 0.0
            crowded = (
                1.0
                if (len(ratio_hist) >= 60 and ratios[idx] > p85 and rel_jump_5d > 0.10)
                else 0.0
            )
            per_avail[avail_dt] = (abn5, abn20, persist20, crowded)

        if not per_avail:
            continue
        a5, a20, ps, cr = per_avail[max(per_avail)]
        if any(not math.isfinite(x) for x in (a5, a20, ps, mm, s60, v20)):
            continue
        abn5_valid[sym] = a5
        abn20_valid[sym] = a20
        persist_valid[sym] = ps
        mom_valid[sym] = mm
        sma_valid[sym] = s60
        vols_valid[sym] = max(v20, 0.16)
        crowd_valid[sym] = cr

    if len(mom_valid) < 15:
        raise ValueError(f"Insufficient Southbound-eligible HK blue chips on {asof_date}")

    z_a5 = _cross_sectional_zscore(abn5_valid)
    z_a20 = _cross_sectional_zscore(abn20_valid)
    z_ps = _cross_sectional_zscore(persist_valid)
    z_mm = _cross_sectional_zscore(mom_valid)

    composite: dict[str, float] = {}
    for sym in mom_valid:
        flow_z = 0.45 * z_a5[sym] + 0.55 * z_a20[sym]
        composite[sym] = (
            0.45 * flow_z + 0.20 * z_ps[sym] + 0.35 * z_mm[sym] - 0.75 * crowd_valid[sym]
        )

    ranked = sorted(composite, key=lambda s: (-composite[s], s))
    rank_of = {sym: r_idx + 1 for r_idx, sym in enumerate(ranked)}
    gross_budget = float(max_gross) * min(1.0, vol_scale * regime_scale)

    actual = {k: v for k, v in (actual_sleeve_weights or {}).items() if v > 0.0}
    currently_held = {s for s, w in actual.items() if w > 0.02}

    selected: list[str] = []
    for sym in sorted(currently_held, key=lambda s: (rank_of.get(s, 999), s)):
        if (
            sym in rank_of
            and rank_of[sym] <= hold_buffer_rank
            and sma_valid[sym] > -0.025
            and composite[sym] > -0.10
            and len(selected) < target_count
        ):
            selected.append(sym)

    for sym in ranked:
        if len(selected) >= target_count:
            break
        if sym in selected:
            continue
        if composite[sym] > 0.15 and sma_valid[sym] > -0.01:
            selected.append(sym)

    raw_weights: dict[str, float] = {}
    if selected and gross_budget >= 0.12:
        inv_vols = {s: 1.0 / vols_valid[s] for s in selected}
        inv_sum = sum(inv_vols.values())
        effective_budget = min(gross_budget, len(selected) * max_single_weight)
        ideal = {
            s: min(max_single_weight, effective_budget * (inv_vols[s] / inv_sum)) for s in selected
        }
        if actual and not regime_changed:
            retained_frozen = {
                s: actual[s]
                for s in selected
                if s in actual and abs(actual[s] - ideal[s]) <= 0.035
            }
            rem_syms = [s for s in selected if s not in retained_frozen]
            rem_budget = max(0.0, effective_budget - sum(retained_frozen.values()))
            raw_weights.update(retained_frozen)
            if rem_syms and rem_budget > 0.02:
                rem_inv_sum = sum(inv_vols[s] for s in rem_syms)
                for s in rem_syms:
                    raw_weights[s] = min(
                        max_single_weight, rem_budget * (inv_vols[s] / rem_inv_sum)
                    )
        else:
            raw_weights = ideal

    proposed = quantize_hk_weights(raw_weights, max_gross)
    retained_drift = False
    if actual:
        same_set = set(proposed) == set(actual)
        all_keys = set(proposed) | set(actual)
        max_diff = max(
            (abs(float(proposed.get(k, Decimal(0))) - actual.get(k, 0.0)) for k in all_keys),
            default=0.0,
        )
        gross_diff = abs(sum(float(v) for v in proposed.values()) - sum(actual.values()))
        if same_set and max_diff < drift_threshold and gross_diff < 0.07:
            retained_drift = True
            proposed = quantize_hk_weights(actual, max_gross)

    diagnostics: dict[str, Any] = {
        "session": asof_date.isoformat(),
        "cold_start": len(currently_held) == 0,
        "regime_scale": round(regime_scale, 3),
        "vol_scale": round(vol_scale, 3),
        "sb_trend_z": round(sb_trend_z, 4),
        "selected": sorted(proposed),
        "retained_via_drift_band": retained_drift,
    }
    return proposed, diagnostics
