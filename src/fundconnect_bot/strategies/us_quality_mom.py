"""Standalone implementation of Strategy 2: `us_sector_quality_mom_v1`."""

from __future__ import annotations

import itertools
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

from ..config import US_EQUITY_STOCKS, US_SECTOR_BY_SYMBOL
from ..data.calendars import parse_sec_available_utc, us_close_and_cutoff
from ..data.indexone import UsBarRecord

WEIGHT_QUANT = Decimal("0.00000001")
INVESTED_BUDGET = Decimal("0.97500000")
FINANCIAL_TTL_DAYS = 180


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


def _compute_residual_mom(
    stock_rets: Sequence[float],
    mkt_rets: Sequence[float],
    rf_rets: Sequence[float],
    window: int,
    skip: int,
) -> float | None:
    """Blitz, Huij & Martens (2011) idiosyncratic residual momentum skipping short-term reversal."""
    if len(stock_rets) < window or len(mkt_rets) < window or len(rf_rets) < window:
        return None
    y = [sr - rr for sr, rr in zip(stock_rets[-window:], rf_rets[-window:], strict=True)]
    x = [mr - rr for mr, rr in zip(mkt_rets[-window:], rf_rets[-window:], strict=True)]
    mean_x = statistics.fmean(x)
    mean_y = statistics.fmean(y)
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    if var_x <= 1e-12:
        beta = 0.0
    else:
        cov_xy = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y, strict=True))
        beta = cov_xy / var_x
    res = [yi - beta * xi for xi, yi in zip(x, y, strict=True)]
    eval_res = res[:-skip] if skip > 0 else res
    if len(eval_res) < 10:
        return None
    denom = math.sqrt(sum(e * e for e in eval_res))
    if denom <= 1e-12:
        return None
    return sum(eval_res) / denom


def _winsorized_zscores(raw_map: Mapping[str, float]) -> dict[str, float]:
    if len(raw_map) < 3:
        return dict.fromkeys(raw_map, 0.0)
    vals = list(raw_map.values())
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    scale = max(mad * 1.4826, statistics.stdev(vals), 1e-8)
    clipped = {k: max(-3.0, min(3.0, (v - med) / scale)) for k, v in raw_map.items()}
    mean_c = statistics.fmean(clipped.values())
    sd_c = statistics.stdev(clipped.values()) if len(clipped) > 1 else 1.0
    if sd_c <= 1e-8:
        return dict.fromkeys(raw_map, 0.0)
    return {k: (v - mean_c) / sd_c for k, v in clipped.items()}


def compute_us_sqm_targets(
    asof_date: date,
    eod_by_symbol: Mapping[str, Sequence[UsBarRecord]],
    fundamentals_rows: Sequence[Mapping[str, Any]],
    *,
    held_stocks: frozenset[str] = frozenset(),
    top_n: int = 10,
    sector_cap: int = 2,
    single_name_cap: float = 0.12,
    retention_bonus: float = 0.20,
) -> tuple[dict[str, Decimal], dict[str, Any]]:
    """Compute `us_sector_quality_mom_v1` sleeve weights as of `asof_date` close."""
    close_utc, _ = us_close_and_cutoff(asof_date)

    spy_bars = [b for b in eod_by_symbol["SPY.US"] if b.session <= asof_date]
    shy_bars = [b for b in eod_by_symbol["SHY.US"] if b.session <= asof_date]
    if not spy_bars or spy_bars[-1].session != asof_date:
        raise ValueError(f"SPY.US lacks EOD bar on {asof_date}")
    hist_dates = [b.session for b in spy_bars]
    spy_closes = [b.adj_close for b in spy_bars]
    shy_closes = [b.adj_close for b in shy_bars]
    spy_rets = [b / a - 1.0 for a, b in itertools.pairwise(spy_closes)]
    shy_rets = [b / a - 1.0 for a, b in itertools.pairwise(shy_closes)]

    # Parse strict SEC EDGAR PIT quarterly reports
    sec_reports_by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in fundamentals_rows:
        sym = str(r.get("mapped_symbol") or r.get("symbol") or "")
        cik = r.get("cik")
        period = str(r.get("period") or "").upper()
        ccy = str(r.get("reportedCurrency") or "").upper()
        if not cik or str(cik).strip() == "" or period not in {"Q1", "Q2", "Q3", "Q4"} or ccy != "USD":
            continue
        avail_utc, rep_date, _ = parse_sec_available_utc(r)
        if avail_utc >= close_utc:
            continue
        sec_reports_by_sym[sym].append(
            {
                "fiscal_year": int(r["fiscalYear"]),
                "quarter": int(period[-1]),
                "report_date": rep_date,
                "available_utc": avail_utc,
                "revenue": float(r["revenue"]) if r.get("revenue") is not None else None,
                "gross_profit": float(r["grossProfit"]) if r.get("grossProfit") is not None else None,
                "operating_income": float(r["operatingIncome"])
                if r.get("operatingIncome") is not None
                else None,
                "net_income": float(r["netIncome"]) if r.get("netIncome") is not None else None,
                "total_assets": float(r["totalAssets"]) if r.get("totalAssets") is not None else None,
                "operating_cash_flow": float(r["operatingCashFlow"])
                if r.get("operatingCashFlow") is not None
                else None,
            }
        )
    for rep_list in sec_reports_by_sym.values():
        rep_list.sort(key=lambda x: (x["available_utc"], x["fiscal_year"], x["quarter"]))

    sma100: dict[str, float | None] = {}
    sma200: dict[str, float | None] = {}
    v10: dict[str, float | None] = {}
    v20: dict[str, float | None] = {}
    v60: dict[str, float | None] = {}
    rm63: dict[str, float | None] = {}
    rm21: dict[str, float | None] = {}
    raw_vam: dict[str, float | None] = {}
    qual_pass: dict[str, float | None] = {}
    prof_map: dict[str, float | None] = {}
    growth_map: dict[str, float | None] = {}
    active_equities: list[str] = []

    for sym in ("SPY.US", "SHY.US", *US_EQUITY_STOCKS):
        bars = [b for b in eod_by_symbol.get(sym, ()) if b.session <= asof_date]
        if not bars or bars[-1].session != asof_date:
            continue
        if [b.session for b in bars] != hist_dates:
            raise ValueError(f"{sym} history sessions do not align with SPY.US through {asof_date}")
        closes = [b.adj_close for b in bars]
        c = closes[-1]
        n_h = len(closes)
        v10[sym] = _compute_log_vol(closes, 10)
        v20[sym] = _compute_log_vol(closes, 20)
        v60[sym] = _compute_log_vol(closes, 60)
        sma100[sym] = c / statistics.fmean(closes[-100:]) - 1.0 if n_h >= 100 else None
        sma200[sym] = c / statistics.fmean(closes[-200:]) - 1.0 if n_h >= 200 else None
        if sym in {"SPY.US", "SHY.US"}:
            continue

        active_equities.append(sym)
        rets = [b / a - 1.0 for a, b in itertools.pairwise(closes)]
        rm63[sym] = _compute_residual_mom(rets, spy_rets, shy_rets, 63, 5)
        rm21[sym] = _compute_residual_mom(rets, spy_rets, shy_rets, 21, 3)
        raw_vam[sym] = (
            0.5
            * ((closes[-6] / closes[-27] - 1.0) + (closes[-6] / closes[-69] - 1.0))
            / max(v20[sym] or 0.15, 0.10)
            if n_h >= 69
            else None
        )

        vis = sec_reports_by_sym.get(sym, [])
        if vis:
            by_yq: dict[tuple[int, int], dict[str, Any]] = {}
            for rep in vis:
                by_yq[(rep["fiscal_year"], rep["quarter"])] = rep
            cur_rep = max(by_yq.values(), key=lambda r: (r["fiscal_year"], r["quarter"]))
            age_days = (asof_date - cur_rep["report_date"]).days
            if age_days <= FINANCIAL_TTL_DAYS:
                ocf = cur_rep["operating_cash_flow"]
                gp = cur_rep["gross_profit"]
                op = cur_rep["operating_income"]
                ni = cur_rep["net_income"]
                ta = cur_rep["total_assets"]
                rev = cur_rep["revenue"]
                pos_ocf = ocf is not None and ocf > 0.0
                pos_earn = any(x is not None and x > 0.0 for x in (op, gp, ni))
                qual_pass[sym] = 1.0 if (pos_ocf and pos_earn) else 0.0
                if ta is not None and ta > 0.0:
                    gp_ta = (gp / ta) if gp is not None else ((op or ni or 0.0) / ta)
                    ocf_ta = (ocf / ta) if ocf is not None else 0.0
                    prof_map[sym] = gp_ta + ocf_ta
                prior = by_yq.get((cur_rep["fiscal_year"] - 1, cur_rep["quarter"]))
                if (
                    prior is not None
                    and rev is not None
                    and rev > 0.0
                    and prior["revenue"] is not None
                    and prior["revenue"] > 0.0
                ):
                    rev_yoy = rev / prior["revenue"] - 1.0
                    cur_margin = (op if op is not None else (gp or ni or 0.0)) / rev
                    prv_margin = (
                        (
                            prior["operating_income"]
                            if prior["operating_income"] is not None
                            else (prior["gross_profit"] or prior["net_income"] or 0.0)
                        )
                        / prior["revenue"]
                    )
                    growth_map[sym] = 0.5 * rev_yoy + 2.0 * (cur_margin - prv_margin)

    above_100_flags = [1.0 if ((sma100.get(s) or -1.0) > 0.0) else 0.0 for s in active_equities]
    breadth_100 = statistics.fmean(above_100_flags) if above_100_flags else 0.0

    pit_eligible: list[str] = []
    mom_raw_scores: dict[str, float] = {}
    prof_raw_scores: dict[str, float] = {}
    growth_raw_scores: dict[str, float] = {}

    for sym in active_equities:
        if qual_pass.get(sym) != 1.0:
            continue
        r63_v = rm63.get(sym)
        r21_v = rm21.get(sym)
        vam_v = raw_vam.get(sym)
        s100_v = sma100.get(sym)
        if r63_v is None or r21_v is None or vam_v is None or s100_v is None:
            continue
        pit_eligible.append(sym)
        mom_raw_scores[sym] = 0.50 * r63_v + 0.30 * r21_v + 0.20 * vam_v
        if prof_map.get(sym) is not None:
            prof_raw_scores[sym] = float(prof_map[sym])  # type: ignore[arg-type]
        if growth_map.get(sym) is not None:
            growth_raw_scores[sym] = float(growth_map[sym])  # type: ignore[arg-type]

    z_mom = _winsorized_zscores(mom_raw_scores)
    z_prof = _winsorized_zscores(prof_raw_scores)
    z_growth = _winsorized_zscores(growth_raw_scores)

    candidates: list[tuple[float, str]] = []
    for sym in pit_eligible:
        s100_v = sma100[sym]
        s200_v = sma200.get(sym)
        assert s100_v is not None
        if s100_v <= -0.02 and (s200_v is None or s200_v <= 0.0):
            continue
        if mom_raw_scores[sym] <= 0.0:
            continue
        fund_z = 0.55 * z_prof.get(sym, 0.0) + 0.45 * z_growth.get(sym, 0.0)
        bonus = retention_bonus if sym in held_stocks else 0.0
        composite = 0.72 * z_mom[sym] + 0.28 * fund_z + bonus
        candidates.append((composite, sym))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected: list[str] = []
    sec_counts: Counter[str] = Counter()
    for _, sym in candidates:
        sec = US_SECTOR_BY_SYMBOL[sym]
        if sec_counts[sec] >= sector_cap:
            continue
        selected.append(sym)
        sec_counts[sec] += 1
        if len(selected) >= top_n:
            break

    spy_s100 = sma100.get("SPY.US") or 0.0
    spy_s200 = sma200.get("SPY.US") or 0.0
    spy_v10 = v10.get("SPY.US") or 0.14
    spy_v20 = v20.get("SPY.US") or 0.14
    spy_v60 = v60.get("SPY.US") or 0.14

    risk_scale = 1.0
    if spy_s100 < 0.0 and spy_v10 > 1.25 * spy_v60:
        risk_scale *= min(0.65, 0.14 / max(spy_v20, 0.10))
    elif spy_v20 > 0.20:
        risk_scale *= min(1.0, 0.18 / spy_v20)

    if spy_s200 < 0.0 and breadth_100 < 0.45:
        risk_scale *= max(0.35, breadth_100 / 0.55)
    elif breadth_100 < 0.40:
        risk_scale *= max(0.50, breadth_100 / 0.50)

    equity_budget = float(INVESTED_BUDGET) * min(1.0, max(0.0, risk_scale))
    raw_weights: dict[str, float] = {}
    if selected and equity_budget > 0.01:
        inv_vols = {sym: 1.0 / max(v20.get(sym) or 0.20, 0.12) for sym in selected}
        inv_sum = sum(inv_vols.values())
        for sym in selected:
            w = equity_budget * (inv_vols[sym] / inv_sum)
            raw_weights[sym] = min(single_name_cap, w)

    eq_used = sum(raw_weights.values())
    shy_weight = max(0.0, float(INVESTED_BUDGET) - eq_used)
    if shy_weight >= 0.005:
        raw_weights["SHY.US"] = shy_weight

    quantized: dict[str, Decimal] = {}
    for sym in sorted(raw_weights):
        qw = _q_weight(raw_weights[sym])
        if qw > 0:
            quantized[sym] = qw
    total_q = sum(quantized.values(), Decimal(0))
    if total_q > INVESTED_BUDGET:
        excess = total_q - INVESTED_BUDGET
        largest = max(quantized, key=lambda k: quantized[k])
        quantized[largest] = quantized[largest] - excess

    diagnostics: dict[str, Any] = {
        "session": asof_date.isoformat(),
        "pit_eligible_count": len(pit_eligible),
        "selected_count": len(selected),
        "selected": selected,
        "sectors": dict(sec_counts),
        "breadth_100": round(breadth_100, 4),
        "risk_scale": round(risk_scale, 4),
        "shy_weight": float(quantized.get("SHY.US", Decimal(0))),
    }
    return quantized, diagnostics
