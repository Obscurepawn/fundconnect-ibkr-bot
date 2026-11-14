"""CLI entry point (`fundconnect-bot`) for weekly planning, Monday simulation, and IBKR execution."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from .config import (
    BOOTSTRAP_SOURCE_SYMBOL,
    HK_TZ,
    NY_TZ,
    ExecutionPolicyConfig,
    IbkrConnectionConfig,
    SleeveWeights,
)
from .data.calendars import latest_completed_week_end
from .execution.broker import (
    CashSnap,
    IbAsyncBrokerAdapter,
    MockBrokerAdapter,
    PositionSnap,
    Quote,
)
from .execution.hkex_rules import round_market_price
from .execution.policy import (
    ExecutionReport,
    compute_portfolio_equity_usd,
    execute_market_rebalance,
)
from .strategies.composite import (
    LiveSleeveState,
    WeeklyPlan,
    build_weekly_plan,
    load_weekly_plan,
    save_weekly_plan,
)

DEFAULT_FALLBACK_US_CACHE = Path("/home/obscure/TradeMaster/data/fundconnect_us")
DEFAULT_FALLBACK_HK_CACHE = Path("/home/obscure/TradeMaster/data/fundconnect_hk")


def _resolve_cache_dirs(
    data_dir: Path,
    us_cache_dir: Path | None,
    hk_cache_dir: Path | None,
) -> tuple[Path, Path]:
    us_dir = us_cache_dir or (
        data_dir
        if (data_dir / "us" / "us_eod.parquet").exists() or (data_dir / "us_eod.parquet").exists()
        else (DEFAULT_FALLBACK_US_CACHE if DEFAULT_FALLBACK_US_CACHE.exists() else data_dir)
    )
    hk_dir = hk_cache_dir or (
        data_dir
        if (data_dir / "hk" / "tushare").exists() or (data_dir / "tushare").exists()
        else (DEFAULT_FALLBACK_HK_CACHE if DEFAULT_FALLBACK_HK_CACHE.exists() else data_dir)
    )
    return us_dir, hk_dir


def _synthetic_quote(
    symbol: str,
    ref_close: float,
    *,
    gap_pct: float = 0.0,
    spread_bps: float = 6.0,
) -> Quote:
    mkt: Literal["US", "HK"] = "HK" if symbol.endswith(".HK") else "US"
    mid = ref_close * (1.0 + gap_pct)
    half = mid * (spread_bps / 20_000.0)
    bid_d = round_market_price(max(0.01, mid - half), "BUY", market=mkt, symbol=symbol)
    ask_d = round_market_price(mid + half, "SELL", market=mkt, symbol=symbol)
    if ask_d < bid_d:
        ask_d = bid_d
    return Quote(
        symbol=symbol,
        bid=float(bid_d),
        ask=float(ask_d),
        last=float(0.5 * (float(bid_d) + float(ask_d))),
        prev_close=ref_close,
    )


def _format_plan_markdown(plan: WeeklyPlan) -> str:
    lines = [
        f"# 周一自动调仓计划书 (`asof={plan.asof_date.isoformat()}`)",
        "",
        f"- **计划哈希 (`plan_sha256`)**: `{plan.plan_sha256}`",
        f"- **港股执行交易日**: `{plan.hk_execution_session.isoformat()}` (`09:32 HKT`, 节假日顺延=`{plan.hk_deferred_holiday}`)",
        f"- **美股执行交易日**: `{plan.us_execution_session.isoformat()}` (美东 `09:32 ET` -> 香港时间 `{plan.us_open_hkt}`, 节假日顺延=`{plan.us_deferred_holiday}`)",
        "- **四策略组合配比**: `35% us_macro_allweather_v1` + `35% us_sector_quality_mom_v1` + `20% hk_etf_southbound_rot_v1` + `10% hk_southbound_bluechip_v1`",
        f"- **目标总仓位**: `{Decimal(plan.summary['total_invested_weight']):.2%}` (美股 `{Decimal(plan.summary['us_invested_weight']):.2%}` + 港股 `{Decimal(plan.summary['hk_invested_weight']):.2%}`，现金缓冲 `{Decimal(plan.summary['total_cash_buffer_weight']):.2%}`)",
        "",
        "| 标的 | IBKR代码 | 市场/交易所 | 币种 | 组合目标权重 | 每手股数 (Lot) | 周五参考收盘价 | 防御回退标的 |",
        "| :--- | :--- | :--- | :--- | ---: | ---: | ---: | :--- |",
    ]
    for t in plan.targets:
        lines.append(
            f"| `{t.symbol}` | `{t.ibkr_symbol}` | `{t.market}/{t.exchange}` | `{t.currency}` | "
            f"`{t.portfolio_weight:.4%}` | `{t.lot_size}` | `{t.ref_close}` | `{t.defensive_fallback}` |"
        )
    return "\n".join(lines)


def _format_execution_markdown(title: str, rep: ExecutionReport) -> str:
    b = rep.batch
    ccy = b.currency
    lines = [
        f"## {title}",
        "",
        f"- **账户总净值估算**: `${b.total_equity_usd:,.2f} USD` (`HK${b.total_equity_hkd:,.2f} HKD` @ `{b.usdhkd_rate:.4f}`)",
        f"- **Phase B 可用无息现金额度 (`{ccy}`)**: `{rep.spendable_cash_before_buys:,.2f} {ccy}`",
        f"- **跳空/风控转入防御标的金额**: `{b.redirected_defensive_notional:,.2f} {ccy}`",
        "",
        "### 1. 逐标的跳空诊断与执行决策",
        "",
        "| 标的 | 当前持仓 | 目标股数 | 净变动 | 周五收盘 | 实时Mid | 跳空幅度 | 点差(bps) | 策略动作 | 订单类型 | 限价 | 分批股数 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- | ---: | :--- |",
    ]
    for d in b.decisions:
        mid_s = f"{d.live_mid:.2f}" if d.live_mid is not None else "N/A"
        gap_s = f"{d.gap_pct:+.2%}" if d.gap_pct is not None else "N/A"
        sp_s = f"{d.spread_bps:.1f}" if d.spread_bps is not None else "N/A"
        lmt_s = str(d.limit_price) if d.limit_price is not None else "-"
        sty_s = d.order_style or "-"
        sl_s = ",".join(str(x) for x in d.slice_quantities) if d.slice_quantities else "-"
        lines.append(
            f"| `{d.symbol}` | {d.current_qty:,} | {d.desired_total_qty:,} | {d.delta_qty:+,} | "
            f"{d.ref_close:.2f} | {mid_s} | {gap_s} | {sp_s} | `{d.action.value}` | `{sty_s}` | {lmt_s} | {sl_s} |"
        )

    lines.extend(
        [
            "",
            f"### 2. 两阶段执行成交汇总 (Phase A 先卖 `{len(rep.phase_a_fills)}` 笔 -> Phase B 后买 `{len(rep.capped_phase_b_buys)}` 笔)",
            "",
        ]
    )
    all_fills = (*rep.phase_a_fills, *rep.phase_b_fills)
    if all_fills:
        lines.extend(
            [
                "| 阶段 | 订单号 | 标的 | 方向 | 委托股数 | 成交股数 | 成交均价 | 最终限价 | 追价次数 | 状态 |",
                "| :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |",
            ]
        )
        for idx, f in enumerate(all_fills):
            ph = "Phase A (Sell)" if idx < len(rep.phase_a_fills) else "Phase B (Buy)"
            lines.append(
                f"| `{ph}` | `{f.order_id}` | `{f.symbol}` | `{f.side}` | {f.requested_qty:,} | "
                f"{f.filled_qty:,} | {f.avg_fill_price:.4f} | {f.final_limit_price} | {f.reprice_count} | `{f.status}` |"
            )
    else:
        lines.append("_本阶段无新增委托订单。_")

    if rep.alerts:
        lines.extend(["", "### 3. 风控与交收告警", ""])
        for al in rep.alerts:
            lines.append(f"- {al}")

    return "\n".join(lines)


def _find_latest_saved_plan(plans_dir: Path) -> Path | None:
    latest_alias = plans_dir / "latest_plan.json"
    if latest_alias.exists():
        return latest_alias
    dated = sorted(plans_dir.glob("plan_*.json"))
    return dated[-1] if dated else None


def _find_previous_plan_before(plans_dir: Path, target_asof: date) -> WeeklyPlan | None:
    """Return the most recent valid saved `plan_*.json` strictly older than `target_asof`."""
    if not plans_dir.exists():
        return None
    for cand_path in reversed(sorted(plans_dir.glob("plan_*.json"))):
        try:
            cand = load_weekly_plan(cand_path)
        except ValueError:
            continue
        if cand.asof_date < target_asof:
            return cand
    return None


def _cmd_plan(args: argparse.Namespace) -> int:
    asof = date.fromisoformat(args.asof) if args.asof else None
    target_asof = asof or latest_completed_week_end("US", datetime.now(UTC).date())
    us_dir, hk_dir = _resolve_cache_dirs(args.data_dir, args.us_cache_dir, args.hk_cache_dir)

    prev_plan = _find_previous_plan_before(args.out_dir, target_asof)

    live_state = LiveSleeveState()
    if args.sync_ibkr_holdings:
        ib_cfg = IbkrConnectionConfig(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            account=args.account,
        )
        with IbAsyncBrokerAdapter(ib_cfg) as broker:
            pos = broker.get_positions()
            cash = broker.get_cash()
            fx = broker.get_usdhkd_rate()
            quotes = broker.get_quotes(list(pos.keys()))
            eq_usd = compute_portfolio_equity_usd(pos, cash, quotes, fx)
            if eq_usd > 0:
                pw: dict[str, float] = {}
                for sym, p in pos.items():
                    q = quotes.get(sym)
                    px = q.mid if (q and q.is_valid) else p.market_price
                    mv_usd = (p.quantity * px) if p.currency == "USD" else (p.quantity * px / fx)
                    pw[sym] = mv_usd / eq_usd
                live_state = LiveSleeveState.from_live_portfolio_weights(
                    pw, previous_plan=prev_plan
                )

    plan = build_weekly_plan(
        asof_date=asof,
        refresh_data=args.refresh_data,
        data_dir=args.data_dir,
        us_cache_dir=us_dir,
        hk_cache_dir=hk_dir,
        sleeve_weights=SleeveWeights(),
        live_state=live_state,
    )
    out_file = args.out_dir / f"plan_{plan.asof_date.strftime('%Y%m%d')}.json"
    save_weekly_plan(plan, out_file)
    save_weekly_plan(plan, args.out_dir / "latest_plan.json")
    print(_format_plan_markdown(plan))
    print(f"\n已保存不可变签名计划文件: {out_file}")
    return 0


def _cmd_simulate_monday(args: argparse.Namespace) -> int:
    if args.plan is not None and args.plan.exists():
        plan = load_weekly_plan(args.plan)
    else:
        asof = date.fromisoformat(args.asof) if args.asof else date(2026, 11, 13)
        us_dir, hk_dir = _resolve_cache_dirs(args.data_dir, args.us_cache_dir, args.hk_cache_dir)
        plan = build_weekly_plan(
            asof_date=asof,
            refresh_data=False,
            data_dir=args.data_dir,
            us_cache_dir=us_dir,
            hk_cache_dir=hk_dir,
        )

    sgov_shares = 9_946
    sgov_px = 100.54
    init_usd_cash = 1_000_000.0 - (sgov_shares * sgov_px)  # $29.16 residual cash -> exact $1,000,000.00
    fx_rate = 7.80
    cfg = ExecutionPolicyConfig(poll_interval_sec=0.0)

    def _build_quotes(overrides: Mapping[str, tuple[float, float]]) -> dict[str, Quote]:
        q_map: dict[str, Quote] = {
            BOOTSTRAP_SOURCE_SYMBOL: _synthetic_quote(
                BOOTSTRAP_SOURCE_SYMBOL, sgov_px, gap_pct=0.0, spread_bps=2.0
            )
        }
        for t in plan.targets:
            gap, sp = overrides.get(t.symbol, (0.001, 5.0 if t.market == "US" else 14.0))
            q_map[t.symbol] = _synthetic_quote(
                t.symbol, float(t.ref_close), gap_pct=gap, spread_bps=sp
            )
        return q_map

    # ---------------- Scenario 1: Normal Smooth Transition from $1,000,000 SGOV.US ----------------
    normal_quotes = _build_quotes({})
    mock_normal = MockBrokerAdapter(
        positions={
            BOOTSTRAP_SOURCE_SYMBOL: PositionSnap(
                BOOTSTRAP_SOURCE_SYMBOL, sgov_shares, sgov_px, sgov_px, "USD"
            )
        },
        cash={
            "USD": CashSnap("USD", init_usd_cash, init_usd_cash, init_usd_cash),
            "HKD": CashSnap("HKD", 0.0, 0.0, 0.0),
        },
        quotes=normal_quotes,
        usdhkd_rate=fx_rate,
    )
    us_rep_normal = execute_market_rebalance(
        plan,
        mock_normal,
        market="US",
        dry_run=False,
        use_adaptive=True,
        bootstrap_fx_usd=300_000.0,
        allow_fx_convert=True,
        config=cfg,
    )
    hk_rep_normal = execute_market_rebalance(
        plan,
        mock_normal,
        market="HK",
        dry_run=False,
        use_adaptive=True,
        config=cfg,
    )

    # ---------------- Scenario 2: Injected Gap-Up / Gap-Down / Wide-Spread Shocks ----------------
    shock_overrides: dict[str, tuple[float, float]] = {
        "LLY.US": (0.045, 8.0),    # +4.5% moderate gap -> 50% staged + 50% to SHY.US
        "AAPL.US": (0.080, 10.0),  # +8.0% extreme surge -> SKIP_GAP_UP + 100% to SHY.US
        "DIS.US": (-0.048, 12.0),  # -4.8% weekend bad news -> SKIP_ADVERSE_GAP_DOWN + 100% to SHY.US
        "0669.HK": (0.072, 22.0),  # +7.2% HK surge -> SKIP_GAP_UP + 100% to 3053.HK
        "9999.HK": (0.008, 90.0),  # 90 bps wide HK spread -> PASSIVE_MID_LIMIT
    }
    shock_quotes = _build_quotes(shock_overrides)
    mock_shock = MockBrokerAdapter(
        positions={
            BOOTSTRAP_SOURCE_SYMBOL: PositionSnap(
                BOOTSTRAP_SOURCE_SYMBOL, sgov_shares, sgov_px, sgov_px, "USD"
            )
        },
        cash={
            "USD": CashSnap("USD", init_usd_cash, init_usd_cash, init_usd_cash),
            "HKD": CashSnap("HKD", 0.0, 0.0, 0.0),
        },
        quotes=shock_quotes,
        usdhkd_rate=fx_rate,
    )
    us_rep_shock = execute_market_rebalance(
        plan,
        mock_shock,
        market="US",
        dry_run=False,
        use_adaptive=True,
        bootstrap_fx_usd=300_000.0,
        allow_fx_convert=True,
        config=cfg,
    )
    hk_rep_shock = execute_market_rebalance(
        plan,
        mock_shock,
        market="HK",
        dry_run=False,
        use_adaptive=True,
        config=cfg,
    )

    sections = [
        _format_plan_markdown(plan),
        "",
        _format_execution_markdown(
            "场景一（正常平稳开盘）：$1,000,000 SGOV.US (9,946 股) -> 美股袖套切换", us_rep_normal
        ),
        "",
        _format_execution_markdown(
            "场景一（正常平稳开盘）：$300,000 换汇结算后 -> 港股袖套切换 (零融资利息)", hk_rep_normal
        ),
        "",
        _format_execution_markdown(
            "场景二（周末新闻跳空压力演练）：美股高开防追高 (+4.5%分步 / +8.0%熔断) 与利空跳空否决 (-4.8%)",
            us_rep_shock,
        ),
        "",
        _format_execution_markdown(
            "场景二（周末新闻跳空与宽点差演练）：港股 0669.HK (+7.2%) 熔断转 3053.HK 与 9999.HK (90bps) 被动中枢限价",
            hk_rep_shock,
        ),
        "",
        "## 零融资利息 (Zero-Margin-Interest) 首次建仓交收指引",
        "",
        "- **Monday 22:32 HKT (`09:32 ET`)**: 执行 `US` 阶段：Phase A 卖出全部 `9,946` 股 `SGOV.US`（`T+1` 周二交收），Phase B 同市场买入 `68.25%` 美股组合（同为 `T+1` 周二交收，**0 融资利息**），保留 `$317,500 USD` 现金。",
        "- **Tuesday 21:30 HKT (美股 `T+1` 现金交收完成后)**: 提交 `IDEALPRO` `USD.HKD` 换汇 `$300,000 USD -> ~HK$2,340,000 HKD`（完全使用已交收美元，避免隔夜货币借贷利息）。",
        "- **港股已交收现金到账后 (`09:32 HKT`)**: 执行 `HK` 阶段，程序自动校验 `SettledCash(HKD)` 上限，按精确每手股数与 HKEX 价位表买入 `3053.HK` 与 7 只南向蓝筹股，全程 **0 港元融资利息**。",
    ]
    print("\n".join(sections))
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    plan_path = args.plan or _find_latest_saved_plan(Path("plans"))
    if plan_path is None or not plan_path.exists():
        raise FileNotFoundError(
            "No plan JSON found; pass --plan plans/plan_YYYYMMDD.json or run `fundconnect-bot plan` first."
        )
    plan = load_weekly_plan(plan_path)
    dry_run = not bool(args.live)
    mkt: Literal["US", "HK"] = "HK" if args.market == "HK" else "US"

    if args.enforce_session_date:
        tz = HK_TZ if mkt == "HK" else NY_TZ
        today_local = datetime.now(tz).date()
        scheduled_sess = plan.hk_execution_session if mkt == "HK" else plan.us_execution_session
        if today_local < scheduled_sess:
            print(
                f"[HOLIDAY_DEFERRED] {mkt} exchange is closed on {today_local.isoformat()}; "
                f"execution automatically deferred to {scheduled_sess.isoformat()}."
            )
            return 0
        if today_local > scheduled_sess:
            print(
                f"[SESSION_ALREADY_PASSED] {mkt} scheduled execution session {scheduled_sess.isoformat()} "
                f"is before today ({today_local.isoformat()}); skipping."
            )
            return 0

    ib_cfg = IbkrConnectionConfig(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        account=args.account,
    )
    pol_cfg = ExecutionPolicyConfig()

    with IbAsyncBrokerAdapter(ib_cfg) as broker:
        rep = execute_market_rebalance(
            plan,
            broker,
            market=mkt,
            dry_run=dry_run,
            use_adaptive=args.use_adaptive,
            bootstrap_fx_usd=args.bootstrap_fx_usd,
            allow_fx_convert=args.allow_fx_convert,
            state_dir=args.state_dir,
            config=pol_cfg,
        )
    mode_label = "DRY-RUN 演练模式 (未发送真实委托)" if dry_run else "LIVE 实盘/模拟盘委托模式"
    print(_format_execution_markdown(f"IBKR {mkt} 执行报告 — {mode_label}", rep))
    if rep.state_path is not None and not dry_run:
        print(f"\n已持久化幂等状态文件: {rep.state_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fundconnect-bot",
        description="Weekly composite portfolio calculator & gap-safe IBKR execution bot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. plan
    p_plan = sub.add_parser("plan", help="Compute immutable weekly Monday plan_YYYYMMDD.json")
    p_plan.add_argument("--asof", type=str, default=None, help="Friday decision date (YYYY-MM-DD)")
    mode_grp = p_plan.add_mutually_exclusive_group()
    mode_grp.add_argument(
        "--refresh-data",
        action="store_true",
        help="Pull fresh live data from IndexOne + Tushare + Cryptoracle",
    )
    mode_grp.add_argument(
        "--from-cache",
        action="store_true",
        help="Evaluate directly from cached Parquet datasets (default)",
    )
    p_plan.add_argument("--data-dir", type=Path, default=Path("data"))
    p_plan.add_argument("--us-cache-dir", type=Path, default=None)
    p_plan.add_argument("--hk-cache-dir", type=Path, default=None)
    p_plan.add_argument("--out-dir", type=Path, default=Path("plans"))
    p_plan.add_argument(
        "--sync-ibkr-holdings",
        action="store_true",
        help="Read live IBKR holdings for week-to-week hysteresis & drift bands",
    )
    p_plan.add_argument("--host", type=str, default="127.0.0.1")
    p_plan.add_argument("--port", type=int, default=4002)
    p_plan.add_argument("--client-id", type=int, default=26)
    p_plan.add_argument("--account", type=str, default="")
    p_plan.set_defaults(func=_cmd_plan)

    # 2. simulate-monday
    p_sim = sub.add_parser(
        "simulate-monday",
        help="Simulate $1,000,000 SGOV.US transition under normal & shock quotes",
    )
    p_sim.add_argument("--plan", type=Path, default=None, help="Optional saved plan_YYYYMMDD.json")
    p_sim.add_argument("--asof", type=str, default="2026-11-13")
    p_sim.add_argument("--data-dir", type=Path, default=Path("data"))
    p_sim.add_argument("--us-cache-dir", type=Path, default=None)
    p_sim.add_argument("--hk-cache-dir", type=Path, default=None)
    p_sim.set_defaults(func=_cmd_simulate_monday)

    # 3. execute
    p_exec = sub.add_parser("execute", help="Execute HK or US sleeve against IBKR TWS / Gateway")
    p_exec.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="Path to plan_YYYYMMDD.json (defaults to plans/latest_plan.json)",
    )
    p_exec.add_argument("--market", choices=["HK", "US"], required=True)
    exec_mode = p_exec.add_mutually_exclusive_group()
    exec_mode.add_argument("--dry-run", action="store_true", default=True)
    exec_mode.add_argument("--live", action="store_true", help="Transmit live orders to IBKR")
    p_exec.add_argument(
        "--enforce-session-date",
        action="store_true",
        help="Only execute when local exchange date matches plan.<market>_execution_session (for Mon..Wed timers)",
    )
    p_exec.add_argument(
        "--use-adaptive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use IBKR Adaptive algo orders on US SMART",
    )
    p_exec.add_argument(
        "--bootstrap-fx-usd",
        type=float,
        default=0.0,
        help="USD amount to reserve/convert into HKD during initial SGOV transition",
    )
    p_exec.add_argument(
        "--allow-fx-convert",
        action="store_true",
        help="Submit IDEALPRO USD.HKD FX conversion order",
    )
    p_exec.add_argument("--state-dir", type=Path, default=Path("state"))
    p_exec.add_argument("--host", type=str, default="127.0.0.1")
    p_exec.add_argument("--port", type=int, default=4002)
    p_exec.add_argument("--client-id", type=int, default=26)
    p_exec.add_argument("--account", type=str, default="")
    p_exec.set_defaults(func=_cmd_execute)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
