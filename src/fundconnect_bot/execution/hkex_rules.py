"""HKEX Spread Table tick rounding, board-lot sizing, and IBKR SEHK symbol normalization.

Implements:
1. HKEX Second Schedule Spread Table:
   - Part A (Equities, incorporating HKEX Phase 1 spread reduction effective 4 Aug 2025:
     10..20 HKD -> 0.01, 20..50 HKD -> 0.02, 50..100 HKD -> 0.05, empirically verified on
     0939.HK=10.31, 0066.HK=32.56, and 0002.HK=81.05).
   - Part B (Fixed Income / Money Market ETPs: 3053.HK CSOP HKD Money Market ETF -> 0.05 HKD,
     empirically verified on 1194.65 / 1195.05 / 1195.60).
   - Part D (HKEX Equity & Gold ETPs: 2800.HK, 2828.HK, 3033.HK, 2823.HK, 3081.HK where
     20..100 HKD uses 0.02 tick, empirically verified on 2828.HK=81.54).
2. `round_hkex_price(price, side, symbol=...)`: floors BUY limits and ceils SELL limits onto
   exact HKEX ticks so orders never violate tick increments (IBKR Error 110 prevention) and
   never exceed maximum buy price / minimum sell floor.
3. Official board-lot rounding (`floor_to_lot`) + live `hk_basic` / `ETF_BOARD_LOTS` lookup.
4. Bidirectional IBKR TWS/Gateway contract symbol mapping (`"0003.HK" <-> ("3", "SEHK", "HKD")`
   stripping leading zeros as required by IBKR SEHK security definitions).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from ..config import (
    DEFAULT_HK_STOCK_BOARD_LOTS,
    ETF_BOARD_LOTS,
    HK_EQUITY_ETFS,
    HK_GOLD_ETF,
    HK_MONEY_ETF,
)

type OrderSide = Literal["BUY", "SELL"]

# Each band is (lower_bound, upper_bound_inclusive, tick_step).
# First band includes 0.01; subsequent bands cover (lower_bound, upper_bound].
HKEX_PART_A_SPREAD_BANDS: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("0.01"), Decimal("0.25"), Decimal("0.001")),
    (Decimal("0.25"), Decimal("0.50"), Decimal("0.005")),
    (Decimal("0.50"), Decimal("10.00"), Decimal("0.01")),
    (Decimal("10.00"), Decimal("20.00"), Decimal("0.01")),
    (Decimal("20.00"), Decimal("50.00"), Decimal("0.02")),
    (Decimal("50.00"), Decimal("100.00"), Decimal("0.05")),
    (Decimal("100.00"), Decimal("200.00"), Decimal("0.10")),
    (Decimal("200.00"), Decimal("500.00"), Decimal("0.20")),
    (Decimal("500.00"), Decimal("1000.00"), Decimal("0.50")),
    (Decimal("1000.00"), Decimal("2000.00"), Decimal("1.00")),
    (Decimal("2000.00"), Decimal("5000.00"), Decimal("2.00")),
    (Decimal("5000.00"), Decimal("9995.00"), Decimal("5.00")),
)

HKEX_ETP_PART_D_SPREAD_BANDS: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("0.01"), Decimal("0.25"), Decimal("0.001")),
    (Decimal("0.25"), Decimal("0.50"), Decimal("0.005")),
    (Decimal("0.50"), Decimal("20.00"), Decimal("0.01")),
    (Decimal("20.00"), Decimal("100.00"), Decimal("0.02")),
    (Decimal("100.00"), Decimal("200.00"), Decimal("0.10")),
    (Decimal("200.00"), Decimal("500.00"), Decimal("0.20")),
    (Decimal("500.00"), Decimal("1000.00"), Decimal("0.50")),
    (Decimal("1000.00"), Decimal("2000.00"), Decimal("1.00")),
    (Decimal("2000.00"), Decimal("5000.00"), Decimal("2.00")),
    (Decimal("5000.00"), Decimal("9995.00"), Decimal("5.00")),
)

_PART_D_ETFS: frozenset[str] = frozenset({*HK_EQUITY_ETFS, HK_GOLD_ETF})
US_TICK = Decimal("0.01")


def _to_dec(value: float | Decimal | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite price: {value}")
        return Decimal(f"{value:.8f}")
    return Decimal(value)


def _select_bands(symbol: str | None) -> tuple[tuple[Decimal, Decimal, Decimal], ...]:
    if symbol is not None and symbol in _PART_D_ETFS:
        return HKEX_ETP_PART_D_SPREAD_BANDS
    return HKEX_PART_A_SPREAD_BANDS


def hkex_tick_size(
    price: float | Decimal,
    *,
    symbol: str | None = None,
    money_market_etf_tick: Decimal = Decimal("0.05"),
) -> Decimal:
    """Return the exact HKEX minimum price variation (tick size) at `price`."""
    d_price = _to_dec(price)
    if d_price < Decimal("0.01") or d_price > Decimal("9995.00"):
        raise ValueError(f"HKEX price {d_price} is outside official [0.01, 9995.00] range")
    if symbol == HK_MONEY_ETF:
        return money_market_etf_tick

    bands = _select_bands(symbol)
    for idx, (low, high, step) in enumerate(bands):
        if idx == 0 and low <= d_price <= high:
            return step
        if low < d_price <= high:
            return step
    raise ValueError(f"Unable to resolve HKEX tick band for price {d_price}")


def round_hkex_price(
    price: float | Decimal,
    side: OrderSide,
    *,
    symbol: str | None = None,
    money_market_etf_tick: Decimal = Decimal("0.05"),
) -> Decimal:
    """Round `price` safely onto an exact HKEX tick (`BUY` -> DOWN, `SELL` -> UP).

    Anchors quantization at the band lower bound so boundary values (e.g. 200.05 BUY -> 200.00)
    never cross into an invalid tick multiple.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid order side: {side}")
    d_price = _to_dec(price)
    if d_price < Decimal("0.01") or d_price > Decimal("9995.00"):
        raise ValueError(f"HKEX price {d_price} is outside official [0.01, 9995.00] range")

    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING

    if symbol == HK_MONEY_ETF:
        step = money_market_etf_tick
        ticks = (d_price / step).to_integral_value(rounding=rounding)
        res = (ticks * step).quantize(step)
        if res < Decimal("0.01"):
            raise ValueError(f"Rounded HKEX price {res} fell below 0.01")
        return res

    bands = _select_bands(symbol)
    for idx, (low, high, step) in enumerate(bands):
        in_band = (low <= d_price <= high) if idx == 0 else (low < d_price <= high)
        if in_band:
            anchor = Decimal(0) if idx == 0 else low
            offset = d_price - anchor
            ticks = (offset / step).to_integral_value(rounding=rounding)
            res = (anchor + ticks * step).quantize(step)
            if res > Decimal("9995.00"):
                return Decimal("9995.00")
            return res
    raise ValueError(f"Unable to round HKEX price {d_price}")


def round_us_price(price: float | Decimal, side: OrderSide) -> Decimal:
    """Round US equity/ETF limit price (>= $1.00 Reg NMS $0.01 tick; BUY down, SELL up)."""
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid order side: {side}")
    d_price = _to_dec(price)
    if d_price <= 0:
        raise ValueError(f"US price must be positive, got {d_price}")
    step = Decimal("0.01") if d_price >= Decimal("1.00") else Decimal("0.0001")
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    ticks = (d_price / step).to_integral_value(rounding=rounding)
    res = (ticks * step).quantize(step)
    if res <= 0:
        return step
    return res


def round_market_price(
    price: float | Decimal,
    side: OrderSide,
    *,
    market: Literal["US", "HK"],
    symbol: str | None = None,
) -> Decimal:
    """Dispatch limit price rounding by market (`US` Reg NMS vs `HK` HKEX Spread Table)."""
    if market == "HK":
        return round_hkex_price(price, side, symbol=symbol)
    return round_us_price(price, side)


def get_hk_board_lot(
    symbol: str,
    *,
    hk_basic_lots: Mapping[str, int] | None = None,
) -> int:
    """Resolve official HKEX board lot (`buy_lot_size`) from `ETF_BOARD_LOTS` or `hk_basic`."""
    if symbol in ETF_BOARD_LOTS:
        return ETF_BOARD_LOTS[symbol]
    if hk_basic_lots is not None and symbol in hk_basic_lots:
        lot = hk_basic_lots[symbol]
        if lot < 1:
            raise ValueError(f"Invalid board lot {lot} for {symbol}")
        return lot
    if symbol in DEFAULT_HK_STOCK_BOARD_LOTS:
        return DEFAULT_HK_STOCK_BOARD_LOTS[symbol]
    raise KeyError(f"Unknown HKEX board lot for {symbol}")


def floor_to_lot(raw_qty: float | Decimal | int, lot_size: int) -> int:
    """Floor a non-negative share count to an integer multiple of `lot_size`."""
    if lot_size < 1:
        raise ValueError(f"lot_size must be >= 1, got {lot_size}")
    val = float(raw_qty)
    if not math.isfinite(val) or val <= 0.0:
        return 0
    lots = math.floor((val + 1e-9) / lot_size)
    return max(0, lots * lot_size)


def split_qty_into_lot_slices(total_qty: int, lot_size: int, num_slices: int) -> list[int]:
    """Split `total_qty` into up to `num_slices` non-zero batches preserving `lot_size`.

    If `total_qty` contains residual odd-lot shares (for full position liquidation), attaches
    any odd-lot remainder to the final slice; for standard board-lot holdings, every slice is
    an exact multiple of `lot_size`.
    """
    if total_qty <= 0:
        return []
    if num_slices <= 1 or total_qty <= lot_size:
        return [total_qty]
    total_lots, odd_rem = divmod(total_qty, lot_size)
    if total_lots == 0:
        return [odd_rem]
    eff_slices = min(num_slices, total_lots)
    base_lots, extra_lots = divmod(total_lots, eff_slices)
    slices = [
        (base_lots + (1 if idx < extra_lots else 0)) * lot_size for idx in range(eff_slices)
    ]
    if odd_rem > 0:
        slices[-1] += odd_rem
    return [q for q in slices if q > 0]


def to_ibkr_contract_fields(symbol: str) -> tuple[str, str, str]:
    """Convert canonical ticker ('0003.HK', 'SHY.US') into IBKR `(symbol, exchange, currency)`.

    Critical IBKR TWS/Gateway rule: SEHK stock & ETF symbols strip all leading zeros
    (e.g. `'0003.HK'` -> `'3'`, `'0002.HK'` -> `'2'`, `'0066.HK'` -> `'66'`, `'0700.HK'` -> `'700'`,
    `'3053.HK'` -> `'3053'`). Submitting `'0003'` on SEHK fails with Error 200.
    """
    code, sep, suffix = symbol.rpartition(".")
    if sep != "." or not code:
        raise ValueError(f"Expected canonical '<CODE>.<US|HK>' symbol, got {symbol!r}")
    mkt = suffix.upper()
    if mkt == "HK":
        if not code.isdigit():
            raise ValueError(f"Numeric HK code expected in {symbol!r}")
        num = int(code)
        if num <= 0:
            raise ValueError(f"Positive HK code expected in {symbol!r}")
        return str(num), "SEHK", "HKD"
    if mkt == "US":
        # IBKR formats US class shares with spaces (e.g. BRK.B -> 'BRK B')
        ibkr_sym = code.upper().replace(".", " ")
        return ibkr_sym, "SMART", "USD"
    raise ValueError(f"Unsupported symbol market suffix in {symbol!r}")


def from_ibkr_position_symbol(ibkr_symbol: str, currency: str) -> str:
    """Normalize an IBKR contract symbol + currency back to canonical `'XXXX.HK'` or `'SYM.US'`."""
    clean = ibkr_symbol.strip().upper()
    ccy = currency.strip().upper()
    if ccy == "HKD":
        if not clean.isdigit():
            raise ValueError(f"Unexpected non-numeric HKD IBKR symbol: {ibkr_symbol!r}")
        num = int(clean)
        return f"{num:04d}.HK" if num < 10000 else f"{num}.HK"
    if ccy == "USD":
        return f"{clean.replace(' ', '.')}.US"
    raise ValueError(f"Unsupported IBKR position currency {currency!r} for {ibkr_symbol!r}")
