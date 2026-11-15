"""IBKR TWS/Gateway (`ib_async`) and deterministic Mock broker adapters."""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, Self, runtime_checkable

from ..config import IbkrConnectionConfig
from .hkex_rules import from_ibkr_position_symbol, round_market_price, to_ibkr_contract_fields

type CurrencyCode = Literal["USD", "HKD"]
type OrderSide = Literal["BUY", "SELL"]
type OrderPhase = Literal["PHASE_A_SELL", "PHASE_B_BUY"]
type OrderStyle = Literal[
    "MARKETABLE_LIMIT",
    "PASSIVE_MID_LIMIT",
    "ADAPTIVE_NORMAL",
    "ADAPTIVE_PATIENT",
    "PROTECTIVE_FLOOR_LIMIT",
]
type OrderStatus = Literal["FILLED", "PARTIAL", "WORKING", "CANCELLED", "DRY_RUN"]


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    prev_close: float
    timestamp_utc: str = ""
    is_delayed: bool = False

    @property
    def is_valid(self) -> bool:
        return (
            math.isfinite(self.bid)
            and math.isfinite(self.ask)
            and self.bid > 0.0
            and self.ask > 0.0
            and self.ask >= self.bid
        )

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0.0:
            return float("inf")
        return ((self.ask - self.bid) / m) * 10_000.0


@dataclass(frozen=True)
class PositionSnap:
    symbol: str
    quantity: int
    avg_cost: float
    market_price: float
    currency: CurrencyCode


@dataclass(frozen=True)
class CashSnap:
    currency: CurrencyCode
    total_cash: float
    settled_cash: float
    available_funds: float


@dataclass(frozen=True)
class OpenOrderSnap:
    order_id: str
    symbol: str
    side: OrderSide
    total_qty: int
    filled_qty: int
    remaining_qty: int
    limit_price: Decimal
    status: str
    client_ref: str = ""


@dataclass(frozen=True)
class OrderSpec:
    intent_id: str
    symbol: str
    ibkr_symbol: str
    exchange: str
    currency: CurrencyCode
    asset_class: Literal["etf", "stock"]
    side: OrderSide
    quantity: int
    lot_size: int
    limit_price: Decimal
    arrival_mid: float
    style: OrderStyle
    phase: OrderPhase
    slice_index: int = 1
    slice_count: int = 1
    reason: str = ""
    delayed_quote: bool = False


@dataclass(frozen=True)
class FillReport:
    order_id: str
    intent_id: str
    symbol: str
    side: OrderSide
    requested_qty: int
    filled_qty: int
    avg_fill_price: float
    final_limit_price: Decimal
    reprice_count: int
    status: OrderStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "requested_qty": self.requested_qty,
            "filled_qty": self.filled_qty,
            "avg_fill_price": round(self.avg_fill_price, 6),
            "final_limit_price": str(self.final_limit_price),
            "reprice_count": self.reprice_count,
            "status": self.status,
        }


@dataclass(frozen=True)
class FxConversionReport:
    pair: str
    sell_currency: CurrencyCode
    buy_currency: CurrencyCode
    sell_amount: float
    estimated_buy_amount: float
    rate: float
    order_id: str
    status: str
    settlement_warning: str


@runtime_checkable
class BrokerAdapter(Protocol):
    """Unified synchronous broker interface for live IBKR Gateway and offline testing."""

    def get_positions(self) -> dict[str, PositionSnap]: ...
    def get_cash(self) -> dict[CurrencyCode, CashSnap]: ...
    def get_open_orders(self) -> list[OpenOrderSnap]: ...
    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]: ...
    def get_usdhkd_rate(self, default: float = 7.80) -> float: ...
    def place_order(self, spec: OrderSpec, *, dry_run: bool = False) -> FillReport: ...
    def modify_order_limit(
        self, order_id: str, spec: OrderSpec, new_limit_price: Decimal
    ) -> FillReport: ...
    def poll_order(self, order_id: str, *, wait_sec: float = 0.0) -> FillReport: ...
    def cancel_order(self, order_id: str) -> None: ...
    def convert_usd_to_hkd(
        self, usd_amount: float, *, limit_rate: float | None = None, dry_run: bool = True
    ) -> FxConversionReport: ...


def _safe_pos_float(val: Any, fallback: float = 0.0) -> float:
    try:
        f = float(val)
        return f if math.isfinite(f) and f > 0.0 else fallback
    except (TypeError, ValueError):
        return fallback


_DEFAULT_IBKR_CONFIG = IbkrConnectionConfig()


class IbAsyncBrokerAdapter:
    """Live IBKR TWS / IB Gateway adapter powered dynamically by `ib_async`.

    Uses `importlib.import_module("ib_async")` at runtime so static typecheckers (`mypy --strict`)
    succeed cleanly in environments without `ib_async` installed.
    """

    def __init__(self, config: IbkrConnectionConfig = _DEFAULT_IBKR_CONFIG) -> None:
        self.config = config
        self._ib_mod: Any = None
        self._ib: Any = None
        self._trades_by_id: dict[str, tuple[Any, OrderSpec, int]] = {}

    def connect(self) -> None:
        try:
            ib_mod = importlib.import_module("ib_async")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ib_async is required for live IBKR connection. Install via: uv pip install ib_async"
            ) from exc
        self._ib_mod = ib_mod
        ib = ib_mod.IB()
        ib.connect(
            host=self.config.host,
            port=self.config.port,
            clientId=self.config.client_id,
            timeout=self.config.timeout_sec,
            account=self.config.account or "",
        )
        if hasattr(ib, "reqMarketDataType"):
            ib.reqMarketDataType(self.config.market_data_type)
        self._ib = ib

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    def _require_ib(self) -> tuple[Any, Any]:
        if self._ib is None or self._ib_mod is None:
            raise RuntimeError("IbAsyncBrokerAdapter is not connected")
        return self._ib_mod, self._ib

    def build_stock_contract(self, symbol: str) -> Any:
        """Construct an `ib_async.Stock` contract stripping SEHK leading zeros (`0003.HK -> '3'`)."""
        ib_mod, _ = self._require_ib()
        ibkr_sym, exch, ccy = to_ibkr_contract_fields(symbol)
        return ib_mod.Stock(symbol=ibkr_sym, exchange=exch, currency=ccy)

    def build_ib_order(self, spec: OrderSpec) -> Any:
        """Construct `LimitOrder` or IBKR `Adaptive` (`Normal` / `Patient`) algo order."""
        ib_mod, _ = self._require_ib()
        order = ib_mod.LimitOrder(
            action=spec.side,
            totalQuantity=spec.quantity,
            lmtPrice=float(spec.limit_price),
            tif="DAY",
            orderRef=spec.intent_id,
            transmit=True,
        )
        if self.config.account:
            order.account = self.config.account
        if spec.exchange == "SMART" and spec.style in ("ADAPTIVE_NORMAL", "ADAPTIVE_PATIENT"):
            priority = "Patient" if spec.style == "ADAPTIVE_PATIENT" else "Normal"
            order.algoStrategy = "Adaptive"
            order.algoParams = [ib_mod.TagValue("adaptivePriority", priority)]
        return order

    def get_positions(self) -> dict[str, PositionSnap]:
        _, ib = self._require_ib()
        out: dict[str, PositionSnap] = {}
        portfolio_items = ib.portfolio(self.config.account) if self.config.account else ib.portfolio()
        for item in portfolio_items:
            sec_type = str(getattr(item.contract, "secType", "STK") or "STK").upper()
            if sec_type not in ("", "STK"):
                continue
            ccy_raw = str(item.contract.currency or "").upper()
            if ccy_raw not in ("USD", "HKD"):
                continue
            qty = round(float(item.position))
            if qty == 0:
                continue
            try:
                canon_sym = from_ibkr_position_symbol(str(item.contract.symbol), ccy_raw)
            except ValueError:
                continue
            ccy: CurrencyCode = "HKD" if ccy_raw == "HKD" else "USD"
            out[canon_sym] = PositionSnap(
                symbol=canon_sym,
                quantity=qty,
                avg_cost=float(item.averageCost or 0.0),
                market_price=float(item.marketPrice or 0.0),
                currency=ccy,
            )
        return out

    def get_cash(self) -> dict[CurrencyCode, CashSnap]:
        _, ib = self._require_ib()
        values = ib.accountValues(self.config.account) if self.config.account else ib.accountValues()
        by_ccy: dict[CurrencyCode, dict[str, float | None]] = {
            "USD": {
                "cash_balance": None,
                "total_cash_balance": None,
                "settled": None,
                "avail": None,
            },
            "HKD": {
                "cash_balance": None,
                "total_cash_balance": None,
                "settled": None,
                "avail": None,
            },
        }
        for av in values:
            ccy_str = str(av.currency or "").upper()
            if ccy_str not in ("USD", "HKD"):
                continue
            ccy: CurrencyCode = "HKD" if ccy_str == "HKD" else "USD"
            tag = str(av.tag or "")
            try:
                val = float(av.value)
            except (TypeError, ValueError):
                continue
            # Never use TotalCashValue (IBKR's base-currency cross-currency aggregate)
            if tag == "CashBalance":
                by_ccy[ccy]["cash_balance"] = val
            elif tag == "TotalCashBalance":
                by_ccy[ccy]["total_cash_balance"] = val
            elif tag == "SettledCash":
                by_ccy[ccy]["settled"] = val
            elif tag == "AvailableFunds":
                by_ccy[ccy]["avail"] = val

        out: dict[CurrencyCode, CashSnap] = {}
        for ccy, m in by_ccy.items():
            cb = m["cash_balance"]
            tcb = m["total_cash_balance"]
            total = cb if cb is not None else (tcb if tcb is not None else 0.0)
            # Preserve explicit 0.0 (or negative) SettledCash rather than overwriting with unsettled total
            settled = m["settled"] if m["settled"] is not None else total
            # IBKR emits AvailableFunds only in account Base Currency; compute_spendable_cash
            # separately enforces SettledCash on HKD when require_settled_hkd=True.
            avail_raw = m["avail"]
            avail = avail_raw if avail_raw is not None else total
            out[ccy] = CashSnap(
                currency=ccy,
                total_cash=total,
                settled_cash=settled,
                available_funds=avail,
            )
        return out

    def get_open_orders(self) -> list[OpenOrderSnap]:
        _, ib = self._require_ib()
        req_all = getattr(ib, "reqAllOpenOrders", None)
        if callable(req_all):
            req_all()
        else:
            ib.reqOpenOrders()
        snaps: list[OpenOrderSnap] = []
        for trade in ib.openTrades():
            ib_status = str(trade.orderStatus.status or "Submitted")
            if ib_status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                continue
            ccy_raw = str(trade.contract.currency or "").upper()
            if ccy_raw not in ("USD", "HKD"):
                continue
            sec_type = str(getattr(trade.contract, "secType", "STK") or "STK").upper()
            raw_sym = str(trade.contract.symbol or "")
            if sec_type == "CASH":
                if (raw_sym.upper(), ccy_raw) != ("USD", "HKD"):
                    continue
                canon = "USD.HKD"
            else:
                try:
                    canon = from_ibkr_position_symbol(raw_sym, ccy_raw)
                except ValueError:
                    continue
            action = str(trade.order.action or "").upper()
            if action not in ("BUY", "SELL"):
                continue
            side: OrderSide = "SELL" if action == "SELL" else "BUY"
            tot = round(float(trade.order.totalQuantity or 0))
            filled = round(float(trade.orderStatus.filled or 0))
            raw_rem = trade.orderStatus.remaining
            rem = max(0, round(float(raw_rem)) if raw_rem is not None else (tot - filled))
            if rem <= 0:
                continue
            lmt = Decimal(str(trade.order.lmtPrice or "0"))
            snaps.append(
                OpenOrderSnap(
                    order_id=str(trade.order.orderId),
                    symbol=canon,
                    side=side,
                    total_qty=tot,
                    filled_qty=filled,
                    remaining_qty=rem,
                    limit_price=lmt,
                    status=ib_status,
                    client_ref=str(trade.order.orderRef or ""),
                )
            )
        return snaps

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        _, ib = self._require_ib()
        unique_syms = list(dict.fromkeys(symbols))
        contracts = [self.build_stock_contract(s) for s in unique_syms]
        if contracts:
            ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts) if contracts else []
        now_iso = datetime.now(UTC).isoformat()
        out: dict[str, Quote] = {}
        for sym, tk in zip(unique_syms, tickers, strict=True):
            raw_mdt = getattr(tk, "marketDataType", 1)
            try:
                tk_mdt = int(raw_mdt) if raw_mdt is not None else 1
            except (TypeError, ValueError):
                tk_mdt = 1
            bid = _safe_pos_float(tk.bid)
            ask = _safe_pos_float(tk.ask)
            d_bid = _safe_pos_float(getattr(tk, "delayedBid", 0.0))
            d_ask = _safe_pos_float(getattr(tk, "delayedAsk", 0.0))
            prev_c = _safe_pos_float(tk.close) or _safe_pos_float(
                getattr(tk, "delayedClose", 0.0)
            )
            raw_last = _safe_pos_float(tk.last) or _safe_pos_float(
                getattr(tk, "delayedLast", 0.0)
            )

            eff_bid = 0.0
            eff_ask = 0.0
            is_delayed = False
            if bid > 0.0 and ask >= bid:
                eff_bid, eff_ask = bid, ask
                is_delayed = tk_mdt in (3, 4)
            elif d_bid > 0.0 and d_ask >= d_bid:
                eff_bid, eff_ask = d_bid, d_ask
                is_delayed = True
            elif raw_last > 0.0:
                mkt: Literal["HK", "US"] = "HK" if sym.endswith(".HK") else "US"
                try:
                    p_lo = float(round_market_price(raw_last, "BUY", market=mkt, symbol=sym))
                    p_hi = float(round_market_price(raw_last, "SELL", market=mkt, symbol=sym))
                    syn_bid = min(p_lo, p_hi)
                    syn_ask = max(p_lo, p_hi)
                    if syn_bid > 0.0 and syn_ask >= syn_bid:
                        eff_bid, eff_ask = syn_bid, syn_ask
                        is_delayed = True
                except ValueError:
                    eff_bid, eff_ask, is_delayed = 0.0, 0.0, False

            eff_last = (
                raw_last
                if raw_last > 0.0
                else (
                    0.5 * (eff_bid + eff_ask) if (eff_bid > 0.0 and eff_ask >= eff_bid) else 0.0
                )
            )
            out[sym] = Quote(
                symbol=sym,
                bid=eff_bid,
                ask=eff_ask,
                last=eff_last,
                prev_close=prev_c,
                timestamp_utc=now_iso,
                is_delayed=is_delayed,
            )
        return out

    def get_usdhkd_rate(self, default: float = 7.80) -> float:
        ib_mod, ib = self._require_ib()
        fx = ib_mod.Forex("USDHKD")
        ib.qualifyContracts(fx)
        tks = ib.reqTickers(fx)
        if not tks:
            return default
        tk = tks[0]
        bid = _safe_pos_float(tk.bid) or _safe_pos_float(getattr(tk, "delayedBid", 0.0))
        ask = _safe_pos_float(tk.ask) or _safe_pos_float(getattr(tk, "delayedAsk", 0.0))
        if bid > 0 and ask >= bid:
            return 0.5 * (bid + ask)
        prev_c = _safe_pos_float(tk.close) or _safe_pos_float(getattr(tk, "delayedClose", 0.0))
        return prev_c if prev_c > 0 else default

    @staticmethod
    def _trade_to_report(
        order_id: str, trade: Any, spec: OrderSpec, reprice_count: int
    ) -> FillReport:
        filled = round(float(trade.orderStatus.filled or 0))
        avg_px = float(trade.orderStatus.avgFillPrice or 0.0)
        ib_status = str(trade.orderStatus.status or "")
        if filled >= spec.quantity or ib_status == "Filled":
            st: OrderStatus = "FILLED"
        elif ib_status in ("Cancelled", "ApiCancelled", "Inactive"):
            st = "CANCELLED"
        elif filled > 0:
            st = "PARTIAL"
        else:
            st = "WORKING"
        return FillReport(
            order_id=order_id,
            intent_id=spec.intent_id,
            symbol=spec.symbol,
            side=spec.side,
            requested_qty=spec.quantity,
            filled_qty=filled,
            avg_fill_price=avg_px,
            final_limit_price=Decimal(str(trade.order.lmtPrice)),
            reprice_count=reprice_count,
            status=st,
        )

    def place_order(self, spec: OrderSpec, *, dry_run: bool = False) -> FillReport:
        if dry_run:
            return FillReport(
                order_id=f"DRY-{spec.intent_id}",
                intent_id=spec.intent_id,
                symbol=spec.symbol,
                side=spec.side,
                requested_qty=spec.quantity,
                filled_qty=0,
                avg_fill_price=0.0,
                final_limit_price=spec.limit_price,
                reprice_count=0,
                status="DRY_RUN",
            )
        _, ib = self._require_ib()
        contract = self.build_stock_contract(spec.symbol)
        ib.qualifyContracts(contract)
        order = self.build_ib_order(spec)
        trade = ib.placeOrder(contract, order)
        ib.sleep(0.5)
        oid = str(trade.order.orderId)
        self._trades_by_id[oid] = (trade, spec, 0)
        return self._trade_to_report(oid, trade, spec, 0)

    def modify_order_limit(
        self, order_id: str, spec: OrderSpec, new_limit_price: Decimal
    ) -> FillReport:
        _, ib = self._require_ib()
        trade, prev_spec, prev_cnt = self._trades_by_id[order_id]
        ib_status = str(trade.orderStatus.status or "")
        filled_now = round(float(trade.orderStatus.filled or 0))
        if (
            ib_status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")
            or filled_now >= spec.quantity
        ):
            return self._trade_to_report(order_id, trade, prev_spec, prev_cnt)

        prev_lmt = trade.order.lmtPrice
        prev_log_len = len(getattr(trade, "log", ()) or ())
        saw_err_104 = False
        oid_int = int(order_id) if order_id.isdigit() else -1

        def _on_error(req_id: Any, error_code: Any, *_: Any) -> None:
            nonlocal saw_err_104
            if req_id == oid_int and error_code == 104:
                saw_err_104 = True

        err_ev = getattr(ib, "errorEvent", None)
        subscribed = False
        if err_ev is not None:
            try:
                err_ev += _on_error
                subscribed = True
            except TypeError:
                subscribed = False
        try:
            trade.order.lmtPrice = float(new_limit_price)
            ib.placeOrder(trade.contract, trade.order)
            ib.sleep(0.5)
        finally:
            if subscribed and err_ev is not None:
                err_ev -= _on_error

        new_logs = (getattr(trade, "log", ()) or ())[prev_log_len:]
        if saw_err_104 or any(
            getattr(entry, "errorCode", 0) == 104
            or "104" in str(getattr(entry, "message", ""))
            for entry in new_logs
        ):
            trade.order.lmtPrice = prev_lmt
            self._trades_by_id[order_id] = (trade, prev_spec, prev_cnt)
            return self._trade_to_report(order_id, trade, prev_spec, prev_cnt)

        updated_spec = replace(spec, limit_price=new_limit_price)
        cnt = prev_cnt + 1
        self._trades_by_id[order_id] = (trade, updated_spec, cnt)
        return self._trade_to_report(order_id, trade, updated_spec, cnt)

    def poll_order(self, order_id: str, *, wait_sec: float = 0.0) -> FillReport:
        _, ib = self._require_ib()
        if wait_sec > 0.0:
            ib.sleep(wait_sec)
        trade, spec, cnt = self._trades_by_id[order_id]
        return self._trade_to_report(order_id, trade, spec, cnt)

    def cancel_order(self, order_id: str) -> None:
        _, ib = self._require_ib()
        if order_id in self._trades_by_id:
            trade, _, _ = self._trades_by_id[order_id]
            ib.cancelOrder(trade.order)
            ib.sleep(0.2)

    def convert_usd_to_hkd(
        self, usd_amount: float, *, limit_rate: float | None = None, dry_run: bool = True
    ) -> FxConversionReport:
        if usd_amount <= 0:
            raise ValueError("usd_amount must be positive")
        rate = limit_rate or self.get_usdhkd_rate()
        est_hkd = usd_amount * rate
        warning = (
            "SETTLEMENT TIMING NOTE: US equity/SGOV sales settle T+1 whereas IDEALPRO USD.HKD "
            "spot FX and SEHK equities settle T+2. Converting USD before SGOV T+1 cash settles "
            "or buying HK stocks before HKD settles on a Margin account can accrue 1-day margin "
            "interest; run HK buys once settled HKD cash is credited for zero margin interest."
        )
        if dry_run:
            return FxConversionReport(
                pair="USD.HKD",
                sell_currency="USD",
                buy_currency="HKD",
                sell_amount=round(usd_amount, 2),
                estimated_buy_amount=round(est_hkd, 2),
                rate=rate,
                order_id="DRY-FX-USDHKD",
                status="DRY_RUN",
                settlement_warning=warning,
            )
        ib_mod, ib = self._require_ib()
        fx_contract = ib_mod.Forex("USDHKD", exchange="FXCONV")
        ib.qualifyContracts(fx_contract)
        # Selling USD.HKD on FXCONV converts physical USD into HKD without creating a virtual FX position.
        fx_order = ib_mod.LimitOrder(
            action="SELL",
            totalQuantity=round(usd_amount),
            lmtPrice=round(rate - 0.0005, 4),
            tif="DAY",
            transmit=True,
        )
        if self.config.account:
            fx_order.account = self.config.account
        trade = ib.placeOrder(fx_contract, fx_order)
        ib.sleep(1.0)
        return FxConversionReport(
            pair="USD.HKD",
            sell_currency="USD",
            buy_currency="HKD",
            sell_amount=round(usd_amount),
            estimated_buy_amount=round(round(usd_amount) * rate, 2),
            rate=rate,
            order_id=str(trade.order.orderId),
            status=str(trade.orderStatus.status or "Submitted"),
            settlement_warning=warning,
        )


class MockBrokerAdapter:
    """Deterministic in-memory broker simulator for dry-run previews and pytest suites."""

    def __init__(
        self,
        *,
        positions: Mapping[str, PositionSnap] | None = None,
        cash: Mapping[CurrencyCode, CashSnap] | None = None,
        quotes: Mapping[str, Quote] | None = None,
        open_orders: Sequence[OpenOrderSnap] | None = None,
        usdhkd_rate: float = 7.80,
        fill_policy: Callable[[OrderSpec, Quote, int], tuple[int, float]] | None = None,
    ) -> None:
        self.positions: dict[str, PositionSnap] = dict(positions or {})
        self.cash: dict[CurrencyCode, CashSnap] = {
            "USD": CashSnap("USD", 0.0, 0.0, 0.0),
            "HKD": CashSnap("HKD", 0.0, 0.0, 0.0),
            **dict(cash or {}),
        }
        self.quotes: dict[str, Quote] = dict(quotes or {})
        self.open_orders: list[OpenOrderSnap] = list(open_orders or [])
        self.usdhkd_rate = usdhkd_rate
        self._fill_policy = fill_policy
        self._next_oid = 1001
        self.submitted_orders: list[OrderSpec] = []
        self.reprice_history: list[tuple[str, str, Decimal]] = []
        self._reports: dict[str, FillReport] = {}
        self._specs: dict[str, OrderSpec] = {}

    def get_positions(self) -> dict[str, PositionSnap]:
        return dict(self.positions)

    def get_cash(self) -> dict[CurrencyCode, CashSnap]:
        return dict(self.cash)

    def get_open_orders(self) -> list[OpenOrderSnap]:
        return list(self.open_orders)

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def get_usdhkd_rate(self, default: float = 7.80) -> float:
        return self.usdhkd_rate or default

    def _apply_fill_accounting(
        self, spec: OrderSpec, incremental_qty: int, fill_px: float
    ) -> None:
        if incremental_qty <= 0:
            return
        ccy = spec.currency
        cur_cash = self.cash[ccy]
        fee_rate = 0.0001 if ccy == "USD" else (0.00015 if spec.asset_class == "etf" else 0.0011)
        notional = incremental_qty * fill_px
        fee = notional * fee_rate

        cur_pos = self.positions.get(spec.symbol)
        old_qty = cur_pos.quantity if cur_pos else 0

        if spec.side == "SELL":
            proceeds = notional - fee
            # Intra-market same-settlement sell proceeds become available in the same currency
            self.cash[ccy] = CashSnap(
                currency=ccy,
                total_cash=cur_cash.total_cash + proceeds,
                settled_cash=cur_cash.settled_cash + (proceeds if ccy == "USD" else 0.0),
                available_funds=cur_cash.available_funds + proceeds,
            )
            new_qty = max(0, old_qty - incremental_qty)
            if new_qty == 0:
                self.positions.pop(spec.symbol, None)
            elif cur_pos is not None:
                self.positions[spec.symbol] = replace(
                    cur_pos, quantity=new_qty, market_price=fill_px
                )
        else:
            outlay = notional + fee
            self.cash[ccy] = CashSnap(
                currency=ccy,
                total_cash=cur_cash.total_cash - outlay,
                settled_cash=max(0.0, cur_cash.settled_cash - outlay),
                available_funds=cur_cash.available_funds - outlay,
            )
            new_qty = old_qty + incremental_qty
            avg_c = (
                ((old_qty * cur_pos.avg_cost) + outlay) / new_qty
                if cur_pos is not None and old_qty > 0
                else outlay / incremental_qty
            )
            self.positions[spec.symbol] = PositionSnap(
                symbol=spec.symbol,
                quantity=new_qty,
                avg_cost=avg_c,
                market_price=fill_px,
                currency=ccy,
            )

    def _evaluate_fill(self, spec: OrderSpec, reprice_count: int) -> tuple[int, float]:
        q = self.quotes.get(spec.symbol)
        if q is None or not q.is_valid:
            return 0, 0.0
        if self._fill_policy is not None:
            return self._fill_policy(spec, q, reprice_count)
        lmt = float(spec.limit_price)
        if spec.style == "PROTECTIVE_FLOOR_LIMIT" and spec.side == "SELL" and lmt > q.bid:
            return 0, 0.0
        if spec.side == "BUY":
            if lmt >= q.ask:
                return spec.quantity, q.ask
            if lmt >= q.mid:
                return spec.quantity, lmt
            return 0, 0.0
        if lmt <= q.bid:
            return spec.quantity, q.bid
        if lmt <= q.mid:
            return spec.quantity, lmt
        return 0, 0.0

    def place_order(self, spec: OrderSpec, *, dry_run: bool = False) -> FillReport:
        self.submitted_orders.append(spec)
        if dry_run:
            rep = FillReport(
                order_id=f"DRY-{spec.intent_id}",
                intent_id=spec.intent_id,
                symbol=spec.symbol,
                side=spec.side,
                requested_qty=spec.quantity,
                filled_qty=0,
                avg_fill_price=0.0,
                final_limit_price=spec.limit_price,
                reprice_count=0,
                status="DRY_RUN",
            )
            self._reports[rep.order_id] = rep
            return rep

        oid = f"MOCK-{self._next_oid}"
        self._next_oid += 1
        filled, px = self._evaluate_fill(spec, 0)
        filled = min(spec.quantity, max(0, filled))
        self._apply_fill_accounting(spec, filled, px)
        st: OrderStatus = (
            "FILLED" if filled >= spec.quantity else ("PARTIAL" if filled > 0 else "WORKING")
        )
        rep = FillReport(
            order_id=oid,
            intent_id=spec.intent_id,
            symbol=spec.symbol,
            side=spec.side,
            requested_qty=spec.quantity,
            filled_qty=filled,
            avg_fill_price=px if filled > 0 else 0.0,
            final_limit_price=spec.limit_price,
            reprice_count=0,
            status=st,
        )
        self._reports[oid] = rep
        self._specs[oid] = spec
        if st in ("WORKING", "PARTIAL"):
            self.open_orders.append(
                OpenOrderSnap(
                    order_id=oid,
                    symbol=spec.symbol,
                    side=spec.side,
                    total_qty=spec.quantity,
                    filled_qty=filled,
                    remaining_qty=spec.quantity - filled,
                    limit_price=spec.limit_price,
                    status=st,
                    client_ref=spec.intent_id,
                )
            )
        return rep

    def modify_order_limit(
        self, order_id: str, spec: OrderSpec, new_limit_price: Decimal
    ) -> FillReport:
        prev = self._reports[order_id]
        cnt = prev.reprice_count + 1
        updated_spec = replace(spec, limit_price=new_limit_price)
        self._specs[order_id] = updated_spec
        self.reprice_history.append((order_id, spec.symbol, new_limit_price))

        target_cum_fill, px = self._evaluate_fill(updated_spec, cnt)
        target_cum_fill = min(spec.quantity, max(prev.filled_qty, target_cum_fill))
        inc = target_cum_fill - prev.filled_qty
        if inc > 0:
            self._apply_fill_accounting(updated_spec, inc, px)
        total_filled = prev.filled_qty + inc
        avg_px = (
            ((prev.filled_qty * prev.avg_fill_price) + (inc * px)) / total_filled
            if total_filled > 0
            else 0.0
        )
        st: OrderStatus = (
            "FILLED"
            if total_filled >= spec.quantity
            else ("PARTIAL" if total_filled > 0 else "WORKING")
        )
        rep = FillReport(
            order_id=order_id,
            intent_id=spec.intent_id,
            symbol=spec.symbol,
            side=spec.side,
            requested_qty=spec.quantity,
            filled_qty=total_filled,
            avg_fill_price=avg_px,
            final_limit_price=new_limit_price,
            reprice_count=cnt,
            status=st,
        )
        self._reports[order_id] = rep
        self.open_orders = [
            (
                replace(
                    o,
                    filled_qty=total_filled,
                    remaining_qty=spec.quantity - total_filled,
                    limit_price=new_limit_price,
                    status=st,
                )
                if o.order_id == order_id
                else o
            )
            for o in self.open_orders
            if not (o.order_id == order_id and st == "FILLED")
        ]
        return rep

    def poll_order(self, order_id: str, *, wait_sec: float = 0.0) -> FillReport:
        _ = wait_sec
        return self._reports[order_id]

    def cancel_order(self, order_id: str) -> None:
        if order_id in self._reports:
            self._reports[order_id] = replace(self._reports[order_id], status="CANCELLED")
        self.open_orders = [o for o in self.open_orders if o.order_id != order_id]

    def convert_usd_to_hkd(
        self, usd_amount: float, *, limit_rate: float | None = None, dry_run: bool = True
    ) -> FxConversionReport:
        rate = limit_rate or self.usdhkd_rate
        hkd_amt = usd_amount * rate
        warning = (
            "SETTLEMENT TIMING NOTE: US equity/SGOV sales settle T+1 whereas USD.HKD spot FX "
            "and SEHK equities settle T+2. Execute HK buys after HKD settles to guarantee "
            "zero margin interest."
        )
        if not dry_run:
            u = self.cash["USD"]
            h = self.cash["HKD"]
            self.cash["USD"] = CashSnap(
                "USD",
                u.total_cash - usd_amount,
                max(0.0, u.settled_cash - usd_amount),
                u.available_funds - usd_amount,
            )
            self.cash["HKD"] = CashSnap(
                "HKD",
                h.total_cash + hkd_amt,
                h.settled_cash + hkd_amt,
                h.available_funds + hkd_amt,
            )
        return FxConversionReport(
            pair="USD.HKD",
            sell_currency="USD",
            buy_currency="HKD",
            sell_amount=round(usd_amount, 2),
            estimated_buy_amount=round(hkd_amt, 2),
            rate=rate,
            order_id="DRY-FX" if dry_run else f"MOCK-FX-{self._next_oid}",
            status="DRY_RUN" if dry_run else "FILLED",
            settlement_warning=warning,
        )
