"""Configuration models, universes, sleeve weights, and execution safety thresholds."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
HK_TZ = ZoneInfo("Asia/Hong_Kong")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

US_DEFENSIVE_SYMBOL = "SHY.US"
HK_DEFENSIVE_SYMBOL = "3053.HK"
BOOTSTRAP_SOURCE_SYMBOL = "SGOV.US"

# ---------------- US Universes ----------------
US_MACRO_ETFS: tuple[str, ...] = (
    "QQQ.US",
    "SPY.US",
    "SCHD.US",
    "GLD.US",
    "IEF.US",
    "TLT.US",
    "SHY.US",
    "IBIT.US",
    "KWEB.US",
    "ASHR.US",
)

US_SECTOR_GROUPS: Mapping[str, tuple[str, ...]] = {
    "semiconductors": (
        "NVDA.US",
        "AVGO.US",
        "AMD.US",
        "AMAT.US",
        "KLAC.US",
        "QCOM.US",
    ),
    "software_hardware": (
        "AAPL.US",
        "MSFT.US",
        "ORCL.US",
        "CRM.US",
        "ADBE.US",
        "INTU.US",
    ),
    "communication": (
        "GOOGL.US",
        "META.US",
        "NFLX.US",
        "TMUS.US",
        "DIS.US",
        "CMCSA.US",
    ),
    "consumer_discretionary": (
        "AMZN.US",
        "TSLA.US",
        "HD.US",
        "MCD.US",
        "TJX.US",
        "LOW.US",
    ),
    "consumer_staples": (
        "COST.US",
        "WMT.US",
        "PG.US",
        "KO.US",
        "PEP.US",
        "PM.US",
    ),
    "financials": (
        "JPM.US",
        "V.US",
        "MA.US",
        "GS.US",
        "SPGI.US",
        "BLK.US",
    ),
    "healthcare": (
        "LLY.US",
        "UNH.US",
        "ABBV.US",
        "JNJ.US",
        "MRK.US",
        "ISRG.US",
    ),
    "industrials_energy": (
        "CAT.US",
        "HON.US",
        "UNP.US",
        "LMT.US",
        "XOM.US",
        "CVX.US",
    ),
}

US_EQUITY_STOCKS: tuple[str, ...] = tuple(
    sorted(sym for group in US_SECTOR_GROUPS.values() for sym in group)
)
US_SECTOR_BY_SYMBOL: Mapping[str, str] = {
    sym: sector for sector, syms in US_SECTOR_GROUPS.items() for sym in syms
}
ALL_US_SYMBOLS: tuple[str, ...] = tuple(sorted({*US_MACRO_ETFS, *US_EQUITY_STOCKS}))

# ---------------- HKEX Universes ----------------
HK_ETF_UNIVERSE: tuple[str, ...] = (
    "2800.HK",  # Tracker Fund of Hong Kong (HSI)
    "2828.HK",  # Hang Seng Investment HSCEI ETF
    "3033.HK",  # CSOP Hang Seng TECH Index ETF
    "2823.HK",  # iShares FTSE China A50 ETF (HKD counter)
    "3081.HK",  # ValueGold ETF (HKD physical gold)
    "3053.HK",  # CSOP Hong Kong Dollar Money Market ETF
)
HK_EQUITY_ETFS: tuple[str, ...] = ("2800.HK", "2828.HK", "3033.HK", "2823.HK")
HK_GOLD_ETF = "3081.HK"
HK_MONEY_ETF = "3053.HK"
HK_BENCHMARK_ETF = "2800.HK"

ETF_BOARD_LOTS: dict[str, int] = {
    "2800.HK": 500,
    "2828.HK": 200,
    "3033.HK": 200,
    "2823.HK": 100,
    "3081.HK": 100,
    "3053.HK": 1,
}

# Static fallback board lots for the 47 HKD Southbound blue-chip universe (verified from hk_basic).
DEFAULT_HK_STOCK_BOARD_LOTS: dict[str, int] = {
    "0002.HK": 500,
    "0003.HK": 1000,
    "0005.HK": 400,
    "0016.HK": 500,
    "0027.HK": 1000,
    "0066.HK": 500,
    "0175.HK": 1000,
    "0267.HK": 1000,
    "0288.HK": 500,
    "0291.HK": 500,
    "0386.HK": 2000,
    "0388.HK": 100,
    "0669.HK": 100,
    "0688.HK": 500,
    "0700.HK": 100,
    "0762.HK": 2000,
    "0857.HK": 2000,
    "0883.HK": 1000,
    "0939.HK": 1000,
    "0941.HK": 500,
    "0968.HK": 2000,
    "0981.HK": 500,
    "0992.HK": 2000,
    "1024.HK": 100,
    "1088.HK": 500,
    "1093.HK": 2000,
    "1109.HK": 500,
    "1211.HK": 500,
    "1288.HK": 1000,
    "1299.HK": 200,
    "1398.HK": 1000,
    "1810.HK": 200,
    "1898.HK": 2000,
    "2015.HK": 100,
    "2020.HK": 200,
    "2269.HK": 500,
    "2318.HK": 500,
    "2319.HK": 1000,
    "2382.HK": 100,
    "2628.HK": 1000,
    "2899.HK": 2000,
    "3690.HK": 100,
    "3968.HK": 500,
    "3988.HK": 1000,
    "9618.HK": 50,
    "9988.HK": 100,
    "9999.HK": 100,
}

HK_BLUECHIP_UNIVERSE: tuple[str, ...] = tuple(sorted(DEFAULT_HK_STOCK_BOARD_LOTS))
ALL_HK_SYMBOLS: tuple[str, ...] = tuple(sorted({*HK_ETF_UNIVERSE, *HK_BLUECHIP_UNIVERSE}))


@dataclass(frozen=True)
class SleeveWeights:
    """Top-level composite portfolio allocation across the 4 verified sleeves."""

    us_macro: Decimal = Decimal("0.35")
    us_sqm: Decimal = Decimal("0.35")
    hk_etf: Decimal = Decimal("0.20")
    hk_bluechip: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        vals = (self.us_macro, self.us_sqm, self.hk_etf, self.hk_bluechip)
        if any(v < 0 for v in vals):
            raise ValueError("Sleeve weights must be non-negative")
        total = sum(vals, Decimal(0))
        if total != Decimal("1.00"):
            raise ValueError(f"Sleeve weights must sum to 1.00, got {total}")

    @property
    def us_total(self) -> Decimal:
        return self.us_macro + self.us_sqm

    @property
    def hk_total(self) -> Decimal:
        return self.hk_etf + self.hk_bluechip


@dataclass(frozen=True)
class ExecutionPolicyConfig:
    """Gap/news-shock thresholds, spread limits, min notional churn filters, and walk-limit caps."""

    # Minimum trade notional to avoid micro-churn commissions
    min_trade_usd: float = 1_500.0
    min_trade_hkd: float = 12_000.0

    # Buy gap & weekend news-shock classification (gap_pct = (mid - ref_close) / ref_close)
    buy_adverse_gap_min: float = -0.03  # < -3.0% -> SKIP_ADVERSE_GAP_DOWN (falling-knife veto)
    buy_mild_gap_max: float = 0.03      # [-3.0%, +3.0%] -> dynamic resize at live mid
    buy_moderate_gap_max: float = 0.06  # (+3.0%, +6.0%] -> 50% staged entry + 50% defensive
    moderate_stage_ratio: float = 0.50  # > +6.0% -> SKIP_GAP_UP (100% to defensive fallback)

    # Sell gap & anti-panic liquidity guard
    sell_severe_gap_threshold: float = -0.05       # < -5.0% -> 3-slice TWAP / Adaptive exit
    sell_flash_crash_threshold: float = -0.12      # < -12.0% -> protective floor limit & alert
    severe_sell_slices: int = 3

    # Bid-ask spread thresholds (switch to passive mid / Adaptive Patient if exceeded)
    max_spread_bps_us: float = 35.0
    max_spread_bps_hk: float = 60.0

    # Bounded Peg-to-Mid / walk-limit repricing parameters (live Level-1 quotes)
    initial_limit_bps: float = 8.0
    walk_step_bps: float = 6.0
    max_walk_steps: int = 3
    max_slippage_bps: float = 25.0
    poll_interval_sec: float = 2.0

    # Free IBKR 15m delayed-quote (`reqMarketDataType(3|4)`) protective limit parameters
    delayed_initial_limit_bps: float = 25.0
    delayed_walk_step_bps: float = 15.0
    delayed_max_slippage_bps: float = 60.0

    # Zero-margin-interest cash reservation buffers
    us_fee_buffer_bps: float = 15.0   # 1.5 bps commission + safety buffer
    hk_stock_fee_buffer_bps: float = 25.0  # 11 bps stamp/commission + tick rounding safety
    hk_etf_fee_buffer_bps: float = 12.0    # Stamp-duty exempt ETF buffer
    require_settled_hkd: bool = True
    default_usdhkd_rate: float = 7.80


@dataclass(frozen=True)
class IbkrConnectionConfig:
    """IBKR TWS / IB Gateway socket connection settings."""

    host: str = "127.0.0.1"
    port: int = 4002  # 4002 = IB Gateway Paper (4001 Live, 7497 TWS Paper, 7496 TWS Live)
    client_id: int = 26
    account: str = ""
    timeout_sec: float = 15.0
    market_data_type: int = 4  # 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-Frozen (free 15m fallback)

    def __post_init__(self) -> None:
        if self.market_data_type not in (1, 2, 3, 4):
            raise ValueError(
                f"market_data_type must be in (1, 2, 3, 4), got {self.market_data_type}"
            )

    @classmethod
    def from_env(cls) -> IbkrConnectionConfig:
        return cls(
            host=os.environ.get("IBKR_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("IBKR_PORT", "4002").strip() or "4002"),
            client_id=int(os.environ.get("IBKR_CLIENT_ID", "26").strip() or "26"),
            account=os.environ.get("IBKR_ACCOUNT", "").strip(),
            market_data_type=int(
                os.environ.get("IBKR_MARKET_DATA_TYPE", "4").strip() or "4"
            ),
        )
