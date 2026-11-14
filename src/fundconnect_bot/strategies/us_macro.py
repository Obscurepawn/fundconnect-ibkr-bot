"""Standalone implementation of Strategy 1: `us_macro_allweather_v1`."""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from ..config import US_MACRO_ETFS
from ..data.calendars import us_close_and_cutoff
from ..data.indexone import UsBarRecord

WEIGHT_QUANT = Decimal("0.00000001")
INVESTED_BUDGET = Decimal("0.97500000")
CORE_SLEEVES: tuple[str, ...] = ("QQQ.US", "SCHD.US", "GLD.US", "IEF.US")
CHINA_CANDIDATES: tuple[str, ...] = ("KWEB.US", "ASHR.US")
CRYPTO_PUB_LAG = timedelta(minutes=15)


def _q_weight(val: float | Decimal) -> Decimal:
    d = val if isinstance(val, Decimal) else Decimal(f"{val:.10f}")
    if d <= 0:
        return Decimal(0)
    return d.quantize(WEIGHT_QUANT, rounding=ROUND_DOWN)


def _compute_log_vol(closes: Sequence[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    sub = closes[-(window + 1) :]
    log_rets = [math.log(b / a) for a, b in itertools.pairwise(sub)]
    return statistics.stdev(log_rets) * math.sqrt(252.0)


def _zscore_trailing(values: Sequence[float], window: int = 60, min_obs: int = 15) -> float | None:
    sub = values[-window:]
    if len(sub) < min_obs:
        return None
    sd = statistics.stdev(sub)
    if sd <= 1e-12:
        return 0.0
    return (sub[-1] - statistics.fmean(sub)) / sd


def _latest_crypto_features(
    crypto_rows: Sequence[Mapping[str, Any]],
    cutoff_utc: datetime,
) -> tuple[float | None, float | None]:
    """Compute causal Cryptoracle `(emo_z, attn_sent)` strictly available `<= cutoff_utc`."""
    sorted_rows = sorted(crypto_rows, key=lambda r: r["period_end"])
    net_sents: list[float] = []
    log_interacts: list[float] = []
    interact_chg5s: list[float] = []
    latest_emo: float | None = None
    latest_attn: float | None = None

    for r in sorted_rows:
        period_end: datetime = r["period_end"].astimezone(UTC)
        avail_utc = period_end + CRYPTO_PUB_LAG
        emo_z = float(r["emo_z"])
        ns = float(r["net_sentiment"])
        li = math.log1p(float(r["interaction_count"]))
        net_sents.append(ns)
        log_interacts.append(li)
        ns_z60 = _zscore_trailing(net_sents, window=60, min_obs=15)
        if len(log_interacts) >= 6:
            interact_chg5s.append(li - log_interacts[-6])
        ic_z60 = _zscore_trailing(interact_chg5s, window=60, min_obs=15)
        attn_sent = (
            0.40 * emo_z + 0.30 * ns_z60 + 0.30 * ic_z60
            if (ns_z60 is not None and ic_z60 is not None)
            else 0.50 * emo_z + 0.50 * (ns * 2.0)
        )
        if avail_utc <= cutoff_utc:
            latest_emo = emo_z
            latest_attn = attn_sent

    return latest_emo, latest_attn


def compute_us_macro_targets(
    asof_date: date,
    eod_by_symbol: Mapping[str, Sequence[UsBarRecord]],
    crypto_rows: Sequence[Mapping[str, Any]],
    *,
    actual_sleeve_weights: Mapping[str, float] | None = None,
    prev_active_regime: frozenset[str] | None = None,
    drift_band: float = 0.05,
    ibit_cap: float = 0.12,
    china_cap: float = 0.08,
) -> tuple[dict[str, Decimal], dict[str, Any]]:
    """Compute `us_macro_allweather_v1` sleeve weights as of `asof_date` close."""
    shy_bars = [b for b in eod_by_symbol.get("SHY.US", ()) if b.session <= asof_date]
    if not shy_bars or shy_bars[-1].session != asof_date:
        raise ValueError(f"SHY.US lacks EOD bar on {asof_date}")

    _, cutoff_utc = us_close_and_cutoff(asof_date)

    vaa: dict[str, float | None] = {}
    r20: dict[str, float | None] = {}
    r21: dict[str, float | None] = {}
    r63: dict[str, float | None] = {}
    sma50: dict[str, float | None] = {}
    sma100: dict[str, float | None] = {}
    sma200: dict[str, float | None] = {}
    v10: dict[str, float | None] = {}
    v20: dict[str, float | None] = {}
    v60: dict[str, float | None] = {}
    active_syms: set[str] = set()

    for sym in US_MACRO_ETFS:
        bars = [b for b in eod_by_symbol.get(sym, ()) if b.session <= asof_date]
        if not bars or bars[-1].session != asof_date:
            continue
        active_syms.add(sym)
        closes_hist = [b.adj_close for b in bars]
        c = closes_hist[-1]
        n_h = len(closes_hist)
        ret20 = c / closes_hist[-21] - 1.0 if n_h >= 21 else None
        ret21 = c / closes_hist[-22] - 1.0 if n_h >= 22 else None
        ret63 = c / closes_hist[-64] - 1.0 if n_h >= 64 else None
        ret126 = c / closes_hist[-127] - 1.0 if n_h >= 127 else None
        ret250 = c / closes_hist[-251] - 1.0 if n_h >= 251 else None
        vaa[sym] = (
            (12.0 * ret21 + 4.0 * ret63 + 2.0 * ret126 + 1.0 * ret250) / 19.0
            if (ret21 is not None and ret63 is not None and ret126 is not None and ret250 is not None)
            else None
        )
        r20[sym] = ret20
        r21[sym] = ret21
        r63[sym] = ret63
        sma50[sym] = c / statistics.fmean(closes_hist[-50:]) - 1.0 if n_h >= 50 else None
        sma100[sym] = c / statistics.fmean(closes_hist[-100:]) - 1.0 if n_h >= 100 else None
        sma200[sym] = c / statistics.fmean(closes_hist[-200:]) - 1.0 if n_h >= 200 else None
        v10[sym] = _compute_log_vol(closes_hist, 10)
        v20[sym] = _compute_log_vol(closes_hist, 20)
        v60[sym] = _compute_log_vol(closes_hist, 60)

    emo_z, attn_sent = _latest_crypto_features(crypto_rows, cutoff_utc)
    shy_vaa = vaa.get("SHY.US") or 0.0
    shy_r20 = r20.get("SHY.US") or 0.0
    shy_r21 = r21.get("SHY.US") or 0.0

    # 1. Tactical Satellite A: IBIT.US (0% .. 12%)
    w_ibit = 0.0
    ibit_meta: dict[str, Any] = {"active": False}
    if "IBIT.US" in active_syms:
        ib_r20 = r20.get("IBIT.US")
        ib_sma50 = sma50.get("IBIT.US")
        ib_v10 = v10.get("IBIT.US")
        ib_v20 = v20.get("IBIT.US")
        ib_v60 = v60.get("IBIT.US")
        price_trend_ok = (
            ib_r20 is not None
            and ib_sma50 is not None
            and ib_v10 is not None
            and ib_v20 is not None
            and ib_v60 is not None
            and ib_r20 > shy_r20
            and ib_sma50 > 0.0
            and ib_v10 < 1.45 * ib_v60
        )
        if price_trend_ok and ib_v20 is not None:
            if attn_sent is not None and emo_z is not None:
                if attn_sent > -0.25 and emo_z < 2.5:
                    sent_mult = min(1.0, max(0.55, 0.75 + 0.25 * attn_sent))
                    w_ibit = min(ibit_cap, (0.045 / max(ib_v20, 0.25)) * sent_mult)
                    ibit_meta = {
                        "active": True,
                        "mode": "cryptoracle_gated",
                        "attn_sent": round(attn_sent, 4),
                        "emo_z": round(emo_z, 4),
                    }
            else:
                w_ibit = min(0.06, 0.025 / max(ib_v20, 0.25))
                ibit_meta = {"active": True, "mode": "pre_2025_price_only"}

    # 2. Tactical Satellite B: China Trend Sleeve (KWEB.US / ASHR.US, 0% .. 8%)
    w_china: dict[str, float] = {}
    best_china: tuple[float, str, float] | None = None
    for c_sym in CHINA_CANDIDATES:
        if c_sym not in active_syms:
            continue
        c_vaa = vaa.get(c_sym)
        c_r21 = r21.get(c_sym)
        c_r63 = r63.get(c_sym)
        c_sma100 = sma100.get(c_sym)
        c_v10 = v10.get(c_sym)
        c_v20 = v20.get(c_sym)
        c_v60 = v60.get(c_sym)
        if (
            c_vaa is not None
            and c_r21 is not None
            and c_r63 is not None
            and c_sma100 is not None
            and c_v10 is not None
            and c_v20 is not None
            and c_v60 is not None
            and (c_vaa - shy_vaa) > 0.0
            and c_r21 > shy_r21
            and c_sma100 > 0.0
            and c_v10 < 1.40 * c_v60
        ):
            score = (0.6 * c_r21 + 0.4 * c_r63) / max(c_v20, 0.15)
            if best_china is None or score > best_china[0]:
                best_china = (score, c_sym, c_v20)
    if best_china is not None and best_china[0] > 0.0:
        _, c_winner, c_vol = best_china
        w_china[c_winner] = min(china_cap, 0.020 / max(c_vol, 0.15))

    # 3. All-Weather Core Risk-Budgeted Sleeves + PAA Breadth / SHY Safe Haven
    core_budget = float(INVESTED_BUDGET) - w_ibit - sum(w_china.values())
    slot_cap = core_budget / len(CORE_SLEEVES)
    raw_core: dict[str, float] = {}
    n_pos_vaa = 0

    for sym in CORE_SLEEVES:
        s_vaa = vaa.get(sym)
        s_sma200 = sma200.get(sym)
        s_v10 = v10.get(sym) or 0.12
        s_v20 = v20.get(sym) or 0.12
        s_v60 = v60.get(sym) or 0.12
        excess_vaa = (s_vaa - shy_vaa) if s_vaa is not None else -1.0
        above_200 = (s_sma200 is not None) and (s_sma200 > 0.0)
        if excess_vaa > 0.0:
            n_pos_vaa += 1

        var_gov = (
            min(1.0, (s_v60 * s_v60) / max(s_v20 * s_v20, 1e-4))
            if (s_v20 > 1.15 * s_v60 or s_v10 > 1.35 * s_v60)
            else 1.0
        )
        inv_vol_tilt = 0.12 / max(min(s_v60, 0.24), 0.08)

        if excess_vaa > 0.0 and above_200:
            mult = min(1.25, 0.95 * inv_vol_tilt * var_gov)
        elif above_200 or excess_vaa > 0.0:
            mult = min(0.85, 0.65 * inv_vol_tilt * var_gov)
        else:
            mult = 0.0

        if sym == "IEF.US" and mult > 0.0 and "TLT.US" in active_syms:
            tlt_vaa = vaa.get("TLT.US")
            tlt_sma200 = sma200.get("TLT.US")
            tlt_v10 = v10.get("TLT.US") or 0.16
            tlt_v60 = v60.get("TLT.US") or 0.16
            if (
                tlt_vaa is not None
                and s_vaa is not None
                and tlt_vaa > s_vaa > shy_vaa
                and (tlt_sma200 or -1.0) > 0.0
                and tlt_v10 <= tlt_v60
            ):
                alloc = slot_cap * mult
                raw_core["IEF.US"] = 0.65 * alloc
                raw_core["TLT.US"] = 0.35 * alloc
                continue

        if mult > 0.0:
            raw_core[sym] = slot_cap * mult

    if n_pos_vaa == 0:
        paa_keep = 0.25
    elif n_pos_vaa == 1:
        paa_keep = 0.65
    else:
        paa_keep = 1.0

    total_core_raw = sum(raw_core.values())
    scale = min(1.0, core_budget / total_core_raw) * paa_keep if total_core_raw > 0 else 0.0
    scaled_core = {sym: w * scale for sym, w in raw_core.items() if w * scale >= 0.01}

    used = w_ibit + sum(w_china.values()) + sum(scaled_core.values())
    defensive_slack = max(0.0, float(INVESTED_BUDGET) - used)

    combined: dict[str, float] = dict(scaled_core)
    if w_ibit >= 0.01:
        combined["IBIT.US"] = w_ibit
    for c_sym, cw in w_china.items():
        if cw >= 0.01:
            combined[c_sym] = cw

    gld_r21 = r21.get("GLD.US") or -1.0
    if defensive_slack > 0.02 and gld_r21 > shy_r21 and gld_r21 > 0.01:
        cur_gld = combined.get("GLD.US", 0.0)
        gld_add = min(0.20 * defensive_slack, max(0.0, 0.30 - cur_gld))
        if gld_add >= 0.005:
            combined["GLD.US"] = cur_gld + gld_add
            defensive_slack -= gld_add

    if defensive_slack >= 0.005 and "SHY.US" in active_syms:
        combined["SHY.US"] = combined.get("SHY.US", 0.0) + defensive_slack

    quantized: dict[str, Decimal] = {}
    for sym in sorted(combined):
        qw = _q_weight(combined[sym])
        if qw > 0:
            quantized[sym] = qw
    total_q = sum(quantized.values(), Decimal(0))
    if total_q > INVESTED_BUDGET:
        excess = total_q - INVESTED_BUDGET
        largest = max(quantized, key=lambda k: quantized[k])
        quantized[largest] = quantized[largest] - excess

    active_set = frozenset(sym for sym, qw in quantized.items() if qw >= Decimal("0.01"))

    retained_drift = False
    if actual_sleeve_weights is not None and prev_active_regime is not None:
        max_drift = max(
            abs(actual_sleeve_weights.get(s, 0.0) - float(quantized.get(s, Decimal(0))))
            for s in active_syms
        )
        if active_set == prev_active_regime and max_drift < drift_band:
            retained_drift = True
            frozen: dict[str, Decimal] = {}
            for s in sorted(actual_sleeve_weights):
                qw = _q_weight(actual_sleeve_weights[s])
                if qw > 0:
                    frozen[s] = qw
            total_f = sum(frozen.values(), Decimal(0))
            if total_f > INVESTED_BUDGET and frozen:
                largest_f = max(frozen, key=lambda k: frozen[k])
                frozen[largest_f] = frozen[largest_f] - (total_f - INVESTED_BUDGET)
            quantized = frozen

    diagnostics: dict[str, Any] = {
        "session": asof_date.isoformat(),
        "n_pos_vaa": n_pos_vaa,
        "paa_keep": paa_keep,
        "ibit": ibit_meta,
        "china": list(w_china.keys()),
        "active_regime": sorted(active_set),
        "retained_via_drift_band": retained_drift,
        "shy_weight": float(quantized.get("SHY.US", Decimal(0))),
    }
    return quantized, diagnostics
