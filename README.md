# FundConnect IBKR Automated Rebalancer (`fundconnect-ibkr-bot`)

`fundconnect-ibkr-bot` is a standalone, zero-external-framework Python package that computes weekly Monday target allocations for a four-sleeve US/HK multi-asset composite and executes smooth, gap-protected rebalances against Interactive Brokers TWS or IB Gateway (`ib_async`). The repository has **zero runtime or import dependencies on `trademaster`**, allowing you to relocate, clone, or deploy it on any Linux/macOS host independently.

---

## 1. Composite Portfolio Architecture

At each completed weekly cutoff (Friday close for Monday rebalances by default, or Thursday close via `--rebalance-day friday` for Friday `09:45 HKT` HK / `22:45 HKT` US rebalances), `fundconnect-bot plan` evaluates four point-in-time (PIT) quantitative sleeves under the **Hybrid C** rebalancing cadence (default `--cadence hybrid-c`: `+52.75%` empirical backtest return, Sharpe `2.097`, MaxDD `-6.09%`; pass `--cadence uniform-1w` for weekly evaluation on all four sleeves) and emits an immutable, SHA-256-verified execution plan (`plan_YYYYMMDD.json` and `plans/latest_plan.json`):

| Sleeve ID | Portfolio Weight | Cadence | Universe & Causal Data Inputs | Defensive Safe Haven |
| :--- | ---: | :---: | :--- | :--- |
| `us_macro_allweather_v1` | **35%** | **2W** (`US_AW`) | 10 US Macro/Tactical ETFs (`QQQ`, `SPY`, `SCHD`, `GLD`, `IEF`, `TLT`, `SHY`, `IBIT`, `KWEB`, `ASHR`) + Cryptoracle BTC 1d sentiment (`+15m` lag) | `SHY.US` |
| `us_sector_quality_mom_v1` | **35%** | **1W** (`US_SQM`) | 48 US large-cap equities across 8 sector groups + strict SEC EDGAR `acceptedDate` PIT quarterly fundamentals (`180d` TTL) | `SHY.US` |
| `hk_etf_southbound_rot_v1` | **20%** | **1W** (`HK_ETF`) | 6 HKD-counter HKEX ETFs (`2800.HK`, `2828.HK`, `3033.HK`, `2823.HK`, `3081.HK`, `3053.HK`) + Tushare Pro Southbound CCASS/GGT breadth (`T+1 08:30 HKT`) | `3053.HK` |
| `hk_southbound_bluechip_v1` | **10%** | **2W** (`HK_BC`) | 47 HKD-counter Southbound blue chips + CCASS `hk_hold` abnormal flow, persistence, and anti-crowding filters | `3053.HK` |

In **Hybrid C**, `us_macro_allweather_v1` (`2W US_AW`) and `hk_southbound_bluechip_v1` (`2W HK_BC`) freeze their prior sleeve-relative target weights on off-weeks (`HELD_2W_FREEZE`, `11`-day shortened-week threshold) whenever `--out-dir` contains a strictly older `plan_*.json` (even without `--sync-ibkr-holdings`), while `us_sector_quality_mom_v1` (`1W US_SQM`) and `hk_etf_southbound_rot_v1` (`1W HK_ETF`) evaluate every week. When `--sync-ibkr-holdings` is supplied on subsequent weeks, live IBKR holdings and the prior week's plan diagnostics are mapped back into per-sleeve weights so rank-buffer hysteresis (Top-11 HK blue-chip buffer, `+0.20` US SQM retention bonus) and drift-band turnover gates operate seamlessly in live trading. On initial cold start (`0` HK stocks held), the engine automatically runs pure cross-sectional entry selection.

---

## 2. Standalone Quickstart & Installation

Enter the standalone repository (`~/fundconnect-ibkr-bot`), optionally attach a private Git remote, and install into an isolated Python 3.12+ virtual environment:

```bash
cd ~/fundconnect-ibkr-bot

# Optional: attach your private GitHub/GitLab remote
git remote add origin git@github.com:<your-org>/fundconnect-ibkr-bot.git
git push -u origin main

# Create an isolated Python 3.12+ virtual environment and install with IBKR + dev extras
uv venv --python 3.12
uv pip install -e ".[ibkr,dev]"
```

Configure credentials strictly via environment variables (never commit `.env` or hardcode keys). Both CLI commands and `systemd --user` units automatically load `~/.config/fundconnect-bot/env` (`$XDG_CONFIG_HOME/fundconnect-bot/env`) and `./.env` if present:

```bash
mkdir -p ~/.config/fundconnect-bot
cp .env.example ~/.config/fundconnect-bot/env
chmod 600 ~/.config/fundconnect-bot/env
# Edit ~/.config/fundconnect-bot/env (or ./.env) to set INDEXONE_API_KEY, TUSHARE_TOKEN,
# CRYPTORACLE_API_KEY, IBKR_PORT=4002, IBKR_ACCOUNT, and IBKR_MARKET_DATA_TYPE=4
mkdir -p data plans state logs
```

By default, `IBKR_MARKET_DATA_TYPE=4` (`Delayed-Frozen`) enables **zero-cost automated execution without paid IBKR Level-1 market data subscriptions**: unsubscribed accounts receive free 15-minute delayed quotes (`delayedBid` / `delayedAsk` / `delayedLast`), whereas subscribed accounts automatically receive real-time Type-1 quotes per IBKR TWS API rules.

---

## 3. Gap, News-Shock & Smooth Execution State Machine

Rather than blindly firing market orders at Monday's open, `fundconnect_bot.execution.policy` compares live Level-1 quotes (`mid = 0.5 * (bid + ask)`) against Friday's reference close (`gap_pct = (mid - ref_close) / ref_close`) and enforces six protective execution tiers plus microstructure guards:

1. **Buy + Mild Gap (`-3.0% <= gap_pct <= +3.0%`) -> `BUY_NORMAL`**: Dynamically recalculates whole-lot share quantity from live `mid` (`floor_to_lot(target_notional / mid, lot_size)`) so mild gap-ups never overspend cash.
2. **Buy + Moderate Bullish Gap (`+3.0% < gap_pct <= +6.0%`) -> `BUY_STAGED_50`**: Anti-chasing **50% staged entry** via `ADAPTIVE_PATIENT` / `PASSIVE_MID_LIMIT`, routing the unspent 50% budget into `SHY.US` (US) or `3053.HK` (HK).
3. **Buy + Extreme Bullish Surge (`gap_pct > +6.0%`) -> `SKIP_GAP_UP`**: **Chase circuit breaker** cancels chasing and redirects 100% of incremental notional into the market's defensive ETF (`SHY.US` / `3053.HK`).
4. **Buy + Adverse Bearish News Gap (`gap_pct < -3.0%`) -> `SKIP_ADVERSE_GAP_DOWN`**: **Falling-knife veto** treats weekend gap-downs beyond `-3%` as thesis-invalidating news shocks and redirects 100% of incremental notional into `SHY.US` / `3053.HK`.
5. **Sell + Normal / Bullish Gap (`gap_pct >= -5.0%`) -> `SELL_NORMAL`**: Captures gap-up windfalls cleanly via `ADAPTIVE_NORMAL` (US `SMART`) or marketable HKEX limit orders.
6. **Sell + Severe Bearish Gap (`-12.0% <= gap_pct < -5.0%`) -> `SELL_SLICED_TWAP` & Flash-Crash Guard (`< -12.0%`)**: Slices exits into 3 board-lot-aligned patient batches instead of dumping into an illiquid opening air-pocket; if `gap_pct < -12.0%`, `HOLD_FLASH_CRASH_GUARD` places a protective floor limit at `-12%` of Friday's close and emits an alert.

### Additional Microstructure, HKEX & Zero-Margin Safeguards

- **HKEX Spread Table & Board-Lot Compliance (`hkex_rules.py`)**: Rounds `BUY` limits down and `SELL` limits up onto exact HKEX Spread Table ticks across `0.01..9,995.00 HKD` (Part A equities including HKEX Phase 1 spread tiers, Part B `3053.HK` `0.05` tick, and Part D HKEX equity/gold ETFs) to prevent IBKR `Error 110`, while stripping SEHK leading zeros (`"0003.HK" -> Stock("3", "SEHK", "HKD")`) to prevent IBKR `Error 200`.
- **Wide-Spread & Halt Guards**: Halted or crossed quotes (`bid <= 0`, `ask <= 0`, or `ask < bid`) trigger `HALTED_OR_NO_QUOTE` and skip safely; spreads wider than `35 bps` (US) or `60 bps` (HK) automatically downgrade orders to `PASSIVE_MID_LIMIT` / `ADAPTIVE_PATIENT`.
- **Two-Phase Sell-Before-Buy & Bounded Walk-Limit (Live & Zero-Cost Delayed Mode)**: Phase A executes all sells first (including `SGOV.US`) with up to 3 Peg-to-Mid limit reprices capped at `25 bps` max arrival slippage on live quotes before Phase B sizes and caps buys against non-borrowed settled cash. When IBKR serves free 15-minute delayed quotes (`is_delayed=True`), the protective ceiling widens to `25..60 bps` (`15 bps` steps) without clamping to stale 15m bid/ask, while IBKR's server-side `Adaptive` algo (`SMART`) and SEHK matching engine still fill at the **real-time live spread (Price Improvement)** and Phase B reserves the full `60 bps` ceiling in cash capping.
- **Idempotency**: Open IBKR orders (`openTrades`) and `state/execution_state_<YYYYMMDD>_<US|HK>.json` are reconciled before order creation so re-running a CLI or timer never double-submits orders.

---

## 4. CLI Usage & Initial `$1,000,000 SGOV.US` Bootstrap (Zero Margin Interest)

### Step A — Preview Full Monday Transition Offline

```bash
# Generate immutable weekly plan from cached Parquet datasets (or --refresh-data for live APIs)
.venv/bin/fundconnect-bot plan --asof 2026-11-13 --from-cache --out-dir plans

# Simulate $1,000,000 SGOV.US (9,946 shares) transition under normal AND weekend shock scenarios
.venv/bin/fundconnect-bot simulate-monday --plan plans/plan_20261113.json
```

### Step B — Zero-Margin-Interest Initial Bootstrap from `SGOV.US`

Because US equities/ETFs (`SGOV.US`) settle **T+1** whereas `IDEALPRO` `USD.HKD` spot FX and HKEX equities settle **T+2**, converting USD or buying HK stocks *before* US `SGOV.US` sale proceeds settle on a Margin account can accrue 1 day of margin interest. Follow this three-step bootstrap sequence on Week 1 for **strictly zero margin interest**:

```bash
# 1. Monday 08:35 HKT: Compute fresh plan from live APIs
.venv/bin/fundconnect-bot plan --refresh-data --data-dir data --out-dir plans

# 2. Monday 09:48 ET (22:48 HKT in winter / 21:48 HKT in summer):
#    Sell all 9,946 SGOV.US (T+1), buy 68.25% US targets (also T+1 -> 0 interest!),
#    and reserve $300,000 USD unspent for the HK sleeve:
.venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market US --bootstrap-fx-usd 300000 --dry-run
.venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market US --bootstrap-fx-usd 300000 --live

# 3. Tuesday evening (after Monday's SGOV.US T+1 USD cash is settled):
#    Convert settled $300,000 USD -> HKD on IDEALPRO (idempotency skips completed US equity legs),
#    then execute HK leg at 09:48 HKT once SettledCash(HKD) is credited:
.venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market US --bootstrap-fx-usd 300000 --allow-fx-convert --live
.venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market HK --dry-run
.venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market HK --live
```

From Week 2 onward, USD and HKD sleeves rebalance within their own currencies on Mondays without cross-currency settlement lag.

---

## 5. Weekly Automation (`systemd --user` & `crontab`)

### Option 1: `systemd --user` Timers (Recommended — Native US DST & Holiday Deferral)

The units in [`deploy/`](deploy/) read optional `EnvironmentFile`s from both `~/fundconnect-ibkr-bot/.env` and `~/.config/fundconnect-bot/env`, bind execution to `09:48` local exchange time (`09:48:00 Asia/Hong_Kong` and `09:48:00 America/New_York`, which shifts between `21:48 HKT` EDT and `22:48 HKT` EST and ensures free 15m delayed quotes reflect post-open `09:33` continuous prints), and fire `Mon,Tue,Wed` with `--enforce-session-date` so Monday holidays on `XNYS` or `XHKG` automatically defer to Tuesday's open:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/fundconnect-*.service deploy/fundconnect-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fundconnect-plan.timer fundconnect-hk.timer fundconnect-us.timer
systemctl --user list-timers 'fundconnect-*'
```

### Option 2: `crontab` Schedule

If your Linux system uses `cron` with `CRON_TZ` support (note `cli.py` auto-loads `~/.config/fundconnect-bot/env` and `./.env`):

```cron
# 1. Monday 08:35 HKT: Refresh IndexOne + Tushare + Cryptoracle and publish plans/latest_plan.json
CRON_TZ=Asia/Hong_Kong
35 8 * * 1 cd $HOME/fundconnect-ibkr-bot && .venv/bin/fundconnect-bot plan --refresh-data --sync-ibkr-holdings --data-dir data --out-dir plans >> logs/plan.log 2>&1

# 2. Mon-Wed 09:48 HKT: Execute HKEX rebalance on plan.hk_execution_session
48 9 * * 1-3 cd $HOME/fundconnect-ibkr-bot && .venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market HK --live --enforce-session-date --state-dir state >> logs/execute-hk.log 2>&1

# 3. Mon-Wed 09:48 America/New_York (DST-safe): Execute US SMART rebalance on plan.us_execution_session
CRON_TZ=America/New_York
48 9 * * 1-3 cd $HOME/fundconnect-ibkr-bot && .venv/bin/fundconnect-bot execute --plan plans/latest_plan.json --market US --live --use-adaptive --enforce-session-date --state-dir state >> logs/execute-us.log 2>&1
```

---

## 6. Development & Verification Suite

```bash
uv run ruff check src tests
uv run mypy --strict src tests
uv run pytest -q
```
