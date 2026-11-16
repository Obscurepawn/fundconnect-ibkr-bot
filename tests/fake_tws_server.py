"""Stateful, protocol-faithful Mock IBKR TWS / IB Gateway server & drop-in `ib_async` module.

Models official IBKR TWS API and `ib_async` client-wrapper semantics:
1. Multi-`clientId` order ownership:
   - `reqOpenOrders()` returns only open orders belonging to `self.clientId` (except `clientId == 0`,
     which also binds and surfaces manual TWS GUI orders `clientId == -1`, while still omitting
     other non-zero API `clientId`s).
   - `reqAllOpenOrders()` surfaces open orders across ALL `clientId`s and manual TWS GUI orders,
     populating `openTrades()`.
2. Exact US Reg NMS (`$0.01` / `$0.0001`) and HKEX Second Schedule Spread Table (Part A equities,
   Part B `3053.HK` `0.05` tick, Part D equity/gold ETPs) tick validation (`Error 110 -> Inactive`),
   SEHK leading-zero contract validation (`Error 200`), and SEHK board-lot / short / cash checks
   (`Error 201 -> Inactive`).
3. Multi-currency cash & value-date clearing ledger (`CashBalance` trade-date `T+0` vs `SettledCash`
   with `T+1` US equity, `T+2` `USD.HKD` spot FX, and `T+2` SEHK equity settlement, plus `IDEALPRO`
   virtual FX position creation vs `FXCONV` pure physical cash conversion).
4. Deterministic event-loop & race engine driven by `ib.sleep(sec)` and `ib.reqTickers(...)`,
   including `ib_async`'s client-side `assert trade.orderStatus.status not in OrderStatus.DoneStates`
   on in-place `placeOrder` modifications and server-side `Error 104: Can't modify a filled order`.
"""

from __future__ import annotations

import copy
import math
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Literal

from fundconnect_bot.execution.hkex_rules import (
    from_ibkr_position_symbol,
    get_hk_board_lot,
    hkex_tick_size,
)

type CurrencyCode = Literal["USD", "HKD"]
type AccountType = Literal["MARGIN", "CASH"]
type FillMode = Literal["CROSS_SPREAD", "MID_OR_BETTER"]


# ---------------------------------------------------------------------------
# Drop-in `ib_async` Data Models
# ---------------------------------------------------------------------------
@dataclass
class Stock:
    symbol: str = ""
    exchange: str = ""
    currency: str = ""
    primaryExchange: str = ""
    secType: str = "STK"
    conId: int = 0
    pair: str = ""


@dataclass
class Forex:
    pair: str = ""
    exchange: str = "IDEALPRO"
    symbol: str = ""
    currency: str = ""
    secType: str = "CASH"
    conId: int = 0

    def __post_init__(self) -> None:
        clean_pair = self.pair.strip().upper()
        if len(clean_pair) == 6 and not self.symbol and not self.currency:
            self.symbol = clean_pair[:3]
            self.currency = clean_pair[3:]
            self.pair = clean_pair
        elif self.symbol and self.currency and not clean_pair:
            self.pair = f"{self.symbol.upper()}{self.currency.upper()}"


@dataclass(frozen=True)
class TagValue:
    tag: str
    value: str


@dataclass
class LimitOrder:
    action: str
    totalQuantity: float
    lmtPrice: float
    tif: str = "DAY"
    orderRef: str = ""
    transmit: bool = True
    account: str = ""
    algoStrategy: str = ""
    algoParams: list[Any] = field(default_factory=list)
    orderId: int = 0
    clientId: int = 0
    permId: int = 0
    orderType: str = "LMT"


@dataclass
class MarketOrder:
    action: str
    totalQuantity: float
    tif: str = "DAY"
    orderRef: str = ""
    transmit: bool = True
    account: str = ""
    orderId: int = 0
    clientId: int = 0
    permId: int = 0
    lmtPrice: float = 0.0
    orderType: str = "MKT"


@dataclass
class OrderStatus:
    ActiveStates: ClassVar[frozenset[str]] = frozenset(
        {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"}
    )
    DoneStates: ClassVar[frozenset[str]] = frozenset(
        {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
    )

    orderId: int = 0
    status: str = "PendingSubmit"
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0
    permId: int = 0
    clientId: int = 0
    lastFillPrice: float = 0.0


@dataclass(frozen=True)
class TradeLogEntry:
    time_sec: float
    status: str
    message: str = ""
    errorCode: int = 0


@dataclass(frozen=True)
class FillExecution:
    exec_id: str
    shares: float
    price: float
    time_sec: float


@dataclass
class Trade:
    contract: Any
    order: Any
    orderStatus: OrderStatus
    fills: list[FillExecution] = field(default_factory=list)
    log: list[TradeLogEntry] = field(default_factory=list)

    def isDone(self) -> bool:
        return self.orderStatus.status in OrderStatus.DoneStates

    def isActive(self) -> bool:
        return self.orderStatus.status in OrderStatus.ActiveStates


@dataclass
class Position:
    account: str
    contract: Any
    position: float
    avgCost: float


@dataclass
class PortfolioItem:
    contract: Any
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float = 0.0
    realizedPNL: float = 0.0
    account: str = ""


@dataclass
class AccountValue:
    tag: str
    value: str
    currency: str
    account: str = ""
    modelCode: str = ""


@dataclass
class Ticker:
    contract: Any
    bid: float
    ask: float
    last: float
    close: float
    bidSize: float = 100.0
    askSize: float = 100.0
    delayedBid: float = -1.0
    delayedAsk: float = -1.0
    delayedLast: float = -1.0
    delayedClose: float = -1.0
    marketDataType: int = 1


# ---------------------------------------------------------------------------
# Server-Side Ledger, Order & Fault-Injection Structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TwsErrorEvent:
    req_id: int
    error_code: int
    message: str
    client_id: int


class FakeEvent:
    """Minimal `eventkit.Event`-compatible callback list (`+=`, `-=`, `.emit(...)`)."""

    def __init__(self) -> None:
        self._handlers: list[Callable[..., Any]] = []

    def __iadd__(self, handler: Callable[..., Any]) -> FakeEvent:
        if handler not in self._handlers:
            self._handlers.append(handler)
        return self

    def __isub__(self, handler: Callable[..., Any]) -> FakeEvent:
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def emit(self, *args: Any) -> None:
        for handler in list(self._handlers):
            handler(*args)


@dataclass(frozen=True)
class SettlementEntry:
    settle_day: int
    currency: CurrencyCode
    amount: float
    description: str


@dataclass
class ServerQuote:
    bid: float
    ask: float
    last: float
    close: float


@dataclass
class ServerHolding:
    canon_symbol: str
    ibkr_symbol: str
    exchange: str
    currency: CurrencyCode
    asset_class: Literal["etf", "stock"]
    quantity: int
    avg_cost: float
    market_price: float


@dataclass
class OrderScript:
    """Deterministic per-symbol execution/fault script for E2E & race tests."""

    # Maximum cumulative shares allowed to fill at each evaluation count (0 = placement, 1 = step 1...)
    max_cum_fill_by_eval: Mapping[int, int] | None = None
    # Explicit execution price override per evaluation index
    fill_price_override: float | None = None
    # Force an IBKR error rejection on initial placement -> Inactive / ApiCancelled
    reject_on_place: tuple[int, str, str] | None = None  # (error_code, message, terminal_status)
    # Trigger an async fill on the working order inside reqTickers() (pumping event loop before modify)
    async_fill_on_req_tickers: bool = False
    # Trigger an async cancel/reject on the working order inside reqTickers()
    async_cancel_on_req_tickers: bool = False
    # Trigger a wire-level race where order fills on server right as modify placeOrder arrives (Error 104)
    wire_fill_race_on_modify: bool = False
    # Raise ConnectionError (simulated TWS socket drop) after placing or on specific sleep call
    disconnect_after_place: bool = False
    disconnect_on_poll: bool = False


@dataclass
class ServerOrderRecord:
    perm_id: int
    client_id: int
    order_id: int
    account: str
    contract: Any
    canon_symbol: str | None
    order: Any
    status: str
    filled: float = 0.0
    remaining: float = 0.0
    avg_fill_price: float = 0.0
    last_fill_price: float = 0.0
    eval_count: int = 0
    fills: list[FillExecution] = field(default_factory=list)
    log: list[TradeLogEntry] = field(default_factory=list)


def _is_exact_tick_multiple(price: float, step: Decimal, anchor: Decimal = Decimal(0)) -> bool:
    if not math.isfinite(price) or price <= 0.0:
        return False
    # Convert float via str to preserve any rounding precision errors passed by caller
    d_px = Decimal(str(price))
    diff = d_px - anchor
    rem = diff % step
    return rem == Decimal(0)


def _hkex_anchor_and_tick(
    price: float,
    canon_symbol: str,
    tick_override: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    if tick_override is not None:
        return Decimal(0), tick_override
    d_px = Decimal(str(price))
    step = hkex_tick_size(d_px, symbol=canon_symbol)
    return Decimal(0), step


class FakeTwsServer:
    """Deterministic stateful IBKR TWS / IB Gateway server simulator."""

    def __init__(
        self,
        *,
        account_id: str = "DU1234567",
        base_currency: CurrencyCode = "USD",
        account_type: AccountType = "MARGIN",
        usd_cash: float = 0.0,
        usd_settled_cash: float | None = None,
        hkd_cash: float = 0.0,
        hkd_settled_cash: float | None = None,
        usdhkd_bid: float = 7.7995,
        usdhkd_ask: float = 7.8005,
        fill_mode: FillMode = "CROSS_SPREAD",
        apply_fees: bool = True,
        has_live_equity_subscription: bool = True,
        delayed_quotes: Mapping[str, tuple[float, float, float, float]] | None = None,
        reject_fxconv: bool = False,
        emit_ledger_cash_tags: bool = False,
    ) -> None:
        self.account_id = account_id
        self.base_currency: CurrencyCode = base_currency
        self.account_type: AccountType = account_type
        self.fill_mode: FillMode = fill_mode
        self.apply_fees = apply_fees
        self.has_live_equity_subscription: bool = has_live_equity_subscription
        self.reject_fxconv: bool = reject_fxconv
        self.emit_ledger_cash_tags: bool = emit_ledger_cash_tags

        self.current_day: int = 0
        self.clock_sec: float = 0.0

        init_usd_settled = usd_cash if usd_settled_cash is None else usd_settled_cash
        init_hkd_settled = hkd_cash if hkd_settled_cash is None else hkd_settled_cash

        # Trade-date cash balance (updates at T+0 fill)
        self.cash_balance: dict[CurrencyCode, float] = {
            "USD": float(usd_cash),
            "HKD": float(hkd_cash),
        }
        # Cleared cash balance as of current_day
        self._cleared_cash: dict[CurrencyCode, float] = {
            "USD": float(init_usd_settled),
            "HKD": float(init_hkd_settled),
        }
        self.settlement_ledger: list[SettlementEntry] = []
        # If initial cash_balance != init_settled_cash, schedule the pre-existing unsettled delta at T+2
        for ccy, tot, setl in (
            ("USD", float(usd_cash), float(init_usd_settled)),
            ("HKD", float(hkd_cash), float(init_hkd_settled)),
        ):
            ccy_lit: CurrencyCode = "HKD" if ccy == "HKD" else "USD"
            diff = tot - setl
            if abs(diff) > 1e-9:
                self.settlement_ledger.append(
                    SettlementEntry(
                        settle_day=self.current_day + 2,
                        currency=ccy_lit,
                        amount=diff,
                        description=f"initial_unsettled_{ccy}",
                    )
                )

        self.usdhkd_bid = usdhkd_bid
        self.usdhkd_ask = usdhkd_ask

        self.holdings: dict[str, ServerHolding] = {}
        # Virtual FX position created ONLY when Forex orders route to IDEALPRO (not FXCONV)
        self.virtual_fx_usd_position: float = 0.0
        self.virtual_fx_avg_rate: float = 0.0

        self.quotes: dict[str, ServerQuote] = {}
        self.delayed_quotes: dict[str, ServerQuote] = {
            sym: ServerQuote(bid=db, ask=da, last=dl, close=dc)
            for sym, (db, da, dl, dc) in (delayed_quotes or {}).items()
        }
        self.tick_overrides: dict[str, Decimal] = {}
        self.scripts: dict[str, OrderScript] = {}
        self.sleep_hooks: list[Callable[[FakeTwsServer, float], None]] = []

        self._next_perm_id: int = 100_001
        self._next_exec_id: int = 1
        self._next_con_id: int = 50_001
        self._con_ids: dict[tuple[str, str, str, str], int] = {}
        self._next_order_id_by_client: dict[int, int] = {}
        self.orders: list[ServerOrderRecord] = []
        self.errors: list[TwsErrorEvent] = []
        self.warnings: list[TwsErrorEvent] = []
        self._clients: dict[int, FakeIB] = {}

    # ---------------- Configuration & Inspection Helpers ----------------
    @property
    def usdhkd_mid(self) -> float:
        return 0.5 * (self.usdhkd_bid + self.usdhkd_ask)

    def set_position(
        self,
        canon_symbol: str,
        quantity: int,
        avg_cost: float,
        market_price: float | None = None,
        *,
        asset_class: Literal["etf", "stock"] | None = None,
    ) -> None:
        code, _, suffix = canon_symbol.rpartition(".")
        if suffix.upper() == "HK":
            ibkr_sym = str(int(code))
            exch = "SEHK"
            ccy: CurrencyCode = "HKD"
            inferred_cls: Literal["etf", "stock"] = (
                "etf"
                if canon_symbol in {"2800.HK", "2828.HK", "3033.HK", "2823.HK", "3081.HK", "3053.HK"}
                else "stock"
            )
        else:
            ibkr_sym = code.upper().replace(".", " ")
            exch = "SMART"
            ccy = "USD"
            inferred_cls = (
                "etf"
                if canon_symbol.endswith("GOV.US")
                or canon_symbol
                in {
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
                }
                else "stock"
            )
        if quantity == 0:
            self.holdings.pop(canon_symbol, None)
            return
        self.holdings[canon_symbol] = ServerHolding(
            canon_symbol=canon_symbol,
            ibkr_symbol=ibkr_sym,
            exchange=exch,
            currency=ccy,
            asset_class=asset_class or inferred_cls,
            quantity=quantity,
            avg_cost=avg_cost,
            market_price=market_price if market_price is not None else avg_cost,
        )

    def set_quote(
        self,
        canon_symbol: str,
        *,
        bid: float,
        ask: float,
        last: float | None = None,
        close: float | None = None,
    ) -> None:
        eff_last = last if last is not None else (0.5 * (bid + ask) if (bid > 0 and ask > 0) else 0.0)
        eff_close = close if close is not None else eff_last
        self.quotes[canon_symbol] = ServerQuote(
            bid=bid, ask=ask, last=eff_last, close=eff_close
        )
        if canon_symbol in self.holdings and eff_last > 0:
            self.holdings[canon_symbol].market_price = eff_last

    def set_delayed_quote(
        self,
        canon_symbol: str,
        *,
        bid: float,
        ask: float,
        last: float | None = None,
        close: float | None = None,
    ) -> None:
        eff_last = last if last is not None else (0.5 * (bid + ask) if (bid > 0 and ask > 0) else -1.0)
        eff_close = close if close is not None else eff_last
        self.delayed_quotes[canon_symbol] = ServerQuote(
            bid=bid, ask=ask, last=eff_last, close=eff_close
        )

    def set_order_script(self, canon_symbol: str, script: OrderScript) -> None:
        self.scripts[canon_symbol] = script

    def settled_cash(self, currency: CurrencyCode) -> float:
        """Compute IBKR `SettledCash` for `currency` as of `current_day`.

        Reflects cleared cash on or before `current_day`, minus any unmatched cumulative
        net purchase/debit obligation across future settlement value dates `D > current_day`.
        Same-value-date sales offset same-value-date purchases (`T+1 == T+1` on US equities,
        `T+2 == T+2` on SEHK equities), while unsettled inflows never prematurely inflate
        `SettledCash` prior to their settlement date.
        """
        cleared = self._cleared_cash[currency]
        future_days = sorted(
            {e.settle_day for e in self.settlement_ledger if e.currency == currency and e.settle_day > self.current_day}
        )
        cum = 0.0
        worst_outflow = 0.0
        for d in future_days:
            day_net = sum(
                e.amount
                for e in self.settlement_ledger
                if e.currency == currency and e.settle_day == d
            )
            cum += day_net
            if -cum > worst_outflow:
                worst_outflow = -cum
        return cleared - worst_outflow

    def clearing_balance_on_day(self, currency: CurrencyCode, day: int) -> float:
        """Return actual clearing-house settled cash at EOD of `day` (margin-interest basis)."""
        base = self._cleared_cash[currency]
        delta = sum(
            e.amount
            for e in self.settlement_ledger
            if e.currency == currency and self.current_day < e.settle_day <= day
        )
        return base + delta

    def advance_settlement_days(self, days: int = 1) -> None:
        """Advance clearing calendar by `days` business sessions, settling due entries."""
        if days < 0:
            raise ValueError("days must be non-negative")
        target_day = self.current_day + days
        remaining: list[SettlementEntry] = []
        for entry in self.settlement_ledger:
            if entry.settle_day <= target_day:
                self._cleared_cash[entry.currency] += entry.amount
            else:
                remaining.append(entry)
        self.settlement_ledger = remaining
        self.current_day = target_day

    def inject_manual_tws_order(
        self,
        canon_symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: int,
        limit_price: float,
        *,
        order_ref: str = "MANUAL-TWS",
    ) -> ServerOrderRecord:
        """Inject a manual order placed via TWS desktop UI (`clientId = -1`, `orderId = 0`)."""
        code, _, suffix = canon_symbol.rpartition(".")
        if suffix.upper() == "HK":
            contract = Stock(symbol=str(int(code)), exchange="SEHK", currency="HKD")
        else:
            contract = Stock(symbol=code.upper(), exchange="SMART", currency="USD")
        self._qualify_one(contract, client_id=-1)
        perm_id = self._alloc_perm_id()
        order = LimitOrder(
            action=side,
            totalQuantity=float(quantity),
            lmtPrice=limit_price,
            orderRef=order_ref,
            orderId=0,
            clientId=-1,
            permId=perm_id,
        )
        rec = ServerOrderRecord(
            perm_id=perm_id,
            client_id=-1,
            order_id=0,
            account=self.account_id,
            contract=contract,
            canon_symbol=canon_symbol,
            order=order,
            status="Submitted",
            filled=0.0,
            remaining=float(quantity),
        )
        self.orders.append(rec)
        return rec

    def cancel_server_order(self, perm_id: int, *, status: str = "Cancelled") -> None:
        """Simulate TWS/exchange cancelling or expiring an active order out-of-band."""
        for rec in self.orders:
            if rec.perm_id == perm_id and rec.status not in OrderStatus.DoneStates:
                rec.status = status
                rec.log.append(TradeLogEntry(self.clock_sec, status, "Server-side cancel/expiry"))
                self._sync_record_to_clients(rec)

    # ---------------- Internal Protocol Mechanics ----------------
    def _alloc_perm_id(self) -> int:
        pid = self._next_perm_id
        self._next_perm_id += 1
        return pid

    def _alloc_order_id(self, client_id: int) -> int:
        nxt = self._next_order_id_by_client.get(client_id, 1001)
        self._next_order_id_by_client[client_id] = nxt + 1
        return nxt

    def _emit_error(self, req_id: int, code: int, message: str, client_id: int) -> None:
        ev = TwsErrorEvent(req_id=req_id, error_code=code, message=message, client_id=client_id)
        if code in (399,):
            self.warnings.append(ev)
        else:
            self.errors.append(ev)
        matched_rec: ServerOrderRecord | None = None
        if req_id > 0:
            matched_rec = next(
                (r for r in self.orders if r.client_id == client_id and r.order_id == req_id),
                None,
            )
            if matched_rec is not None:
                matched_rec.log.append(
                    TradeLogEntry(self.clock_sec, matched_rec.status, message, errorCode=code)
                )
                self._sync_record_to_clients(matched_rec)
        client = self._clients.get(client_id)
        if client is not None:
            client.errorEvent.emit(
                req_id,
                code,
                message,
                matched_rec.contract if matched_rec is not None else None,
            )

    def _resolve_stock_canon(self, contract: Any, client_id: int, req_id: int) -> str | None:
        sym = str(getattr(contract, "symbol", "") or "").strip()
        exch = str(getattr(contract, "exchange", "") or "").strip().upper()
        ccy = str(getattr(contract, "currency", "") or "").strip().upper()

        if ccy == "HKD" or exch == "SEHK":
            # IBKR SEHK strict rule: leading zeros ('0003') are rejected with Error 200!
            if not sym.isdigit() or (len(sym) > 1 and sym.startswith("0")) or int(sym) <= 0:
                self._emit_error(
                    req_id,
                    200,
                    f"No security definition has been found for SEHK symbol {sym!r}",
                    client_id,
                )
                return None
            canon = from_ibkr_position_symbol(sym, "HKD")
            if (
                canon not in self.quotes
                and canon not in self.delayed_quotes
                and canon not in self.holdings
            ):
                self._emit_error(
                    req_id,
                    200,
                    f"No security definition has been found for {canon}",
                    client_id,
                )
                return None
            return canon

        if ccy == "USD":
            canon = from_ibkr_position_symbol(sym, "USD")
            if (
                canon not in self.quotes
                and canon not in self.delayed_quotes
                and canon not in self.holdings
            ):
                self._emit_error(
                    req_id,
                    200,
                    f"No security definition has been found for {canon}",
                    client_id,
                )
                return None
            return canon

        self._emit_error(req_id, 200, f"Unsupported contract currency {ccy!r}", client_id)
        return None

    def _qualify_one(self, contract: Any, client_id: int) -> bool:
        sec_type = str(getattr(contract, "secType", "STK") or "STK").upper()
        if sec_type == "CASH":
            sym = str(getattr(contract, "symbol", "") or "").upper()
            ccy = str(getattr(contract, "currency", "") or "").upper()
            exch = str(getattr(contract, "exchange", "IDEALPRO") or "IDEALPRO").upper()
            if (
                (sym, ccy) != ("USD", "HKD")
                or exch not in ("IDEALPRO", "FXCONV")
                or (exch == "FXCONV" and self.reject_fxconv)
            ):
                contract.conId = 0
                self._emit_error(
                    0, 200, f"Unknown CASH contract {sym}.{ccy}@{exch}", client_id
                )
                return False
            key = ("CASH", sym, ccy, exch)
            if key not in self._con_ids:
                self._con_ids[key] = self._next_con_id
                self._next_con_id += 1
            contract.conId = self._con_ids[key]
            return True

        canon = self._resolve_stock_canon(contract, client_id=client_id, req_id=0)
        if canon is None:
            contract.conId = 0
            return False
        key = (
            "STK",
            str(contract.symbol).upper(),
            str(contract.currency).upper(),
            str(contract.exchange).upper(),
        )
        if key not in self._con_ids:
            self._con_ids[key] = self._next_con_id
            self._next_con_id += 1
        contract.conId = self._con_ids[key]
        return True

    def _validate_order(self, rec: ServerOrderRecord, *, is_modify: bool) -> bool:
        order = rec.order
        action = str(order.action or "").upper()
        qty = float(order.totalQuantity or 0.0)
        if action not in ("BUY", "SELL") or qty <= 0.0:
            self._emit_error(rec.order_id, 201, "Order rejected - Invalid side or quantity", rec.client_id)
            rec.status = "Inactive"
            return False

        sec_type = str(getattr(rec.contract, "secType", "STK") or "STK").upper()
        if sec_type == "CASH":
            if str(order.orderType).upper() == "LMT" and not _is_exact_tick_multiple(
                float(order.lmtPrice), Decimal("0.00005")
            ):
                # USD.HKD minimum tick on IDEALPRO/FXCONV is 0.00005 / 0.0001
                self._emit_error(
                    rec.order_id,
                    110,
                    "The price does not conform to the minimum price variation for USD.HKD.",
                    rec.client_id,
                )
                rec.status = "Inactive"
                return False
            if str(getattr(rec.contract, "exchange", "IDEALPRO")).upper() == "IDEALPRO" and qty < 25_000:
                self._emit_error(
                    rec.order_id,
                    399,
                    "Order Message: Your order size is below the IdealPro minimum and will be routed as an odd lot order.",
                    rec.client_id,
                )
            return True

        canon = rec.canon_symbol
        if canon is None:
            rec.status = "Inactive"
            return False

        script = self.scripts.get(canon)
        if not is_modify and script is not None and script.reject_on_place is not None:
            err_code, err_msg, term_st = script.reject_on_place
            self._emit_error(rec.order_id, err_code, err_msg, rec.client_id)
            rec.status = term_st
            return False

        # Tick validation (Error 110)
        if str(order.orderType).upper() == "LMT":
            lmt = float(order.lmtPrice)
            override_tick = self.tick_overrides.get(canon)
            if canon.endswith(".HK"):
                try:
                    anchor, step = _hkex_anchor_and_tick(lmt, canon, override_tick)
                except ValueError as exc:
                    self._emit_error(rec.order_id, 110, str(exc), rec.client_id)
                    rec.status = "Inactive"
                    return False
                if not _is_exact_tick_multiple(lmt, step, anchor):
                    self._emit_error(
                        rec.order_id,
                        110,
                        f"The price {lmt} does not conform to the minimum price variation {step} for {canon}.",
                        rec.client_id,
                    )
                    rec.status = "Inactive"
                    return False
            else:
                step = override_tick or (
                    Decimal("0.01") if lmt >= 1.0 else Decimal("0.0001")
                )
                if not _is_exact_tick_multiple(lmt, step):
                    self._emit_error(
                        rec.order_id,
                        110,
                        f"The price {lmt} does not conform to the minimum price variation {step} for {canon}.",
                        rec.client_id,
                    )
                    rec.status = "Inactive"
                    return False

        # SEHK board-lot check on BUY orders (Error 201)
        int_qty = round(qty)
        if canon.endswith(".HK") and action == "BUY":
            try:
                lot = get_hk_board_lot(canon)
            except KeyError:
                lot = 100
            if int_qty % lot != 0:
                self._emit_error(
                    rec.order_id,
                    201,
                    f"Order rejected - Reason: Quantity {int_qty} is not a multiple of SEHK board lot {lot}",
                    rec.client_id,
                )
                rec.status = "Inactive"
                return False

        # Short-sale prevention when not holding enough shares (Error 201)
        if action == "SELL" and not is_modify:
            held_qty = self.holdings[canon].quantity if canon in self.holdings else 0
            already_working_sell = sum(
                round(r.remaining)
                for r in self.orders
                if r.perm_id != rec.perm_id
                and r.canon_symbol == canon
                and str(r.order.action).upper() == "SELL"
                and r.status not in OrderStatus.DoneStates
            )
            if int_qty + already_working_sell > held_qty:
                self._emit_error(
                    rec.order_id,
                    201,
                    f"Order rejected - Reason: Short sale disallowed ({int_qty} > available {held_qty - already_working_sell})",
                    rec.client_id,
                )
                rec.status = "Inactive"
                return False

        # Cash account pre-trade settled-cash check (Error 201)
        if self.account_type == "CASH" and action == "BUY":
            ccy: CurrencyCode = "HKD" if canon.endswith(".HK") else "USD"
            est_px = float(order.lmtPrice) if str(order.orderType).upper() == "LMT" else self.quotes[canon].ask
            if int_qty * est_px > self.settled_cash(ccy) + 1e-6:
                self._emit_error(
                    rec.order_id,
                    201,
                    f"Order rejected - Reason: Cash account settled {ccy} insufficient",
                    rec.client_id,
                )
                rec.status = "Inactive"
                return False

        return True

    def _fee_rate_for(self, canon_symbol: str) -> float:
        if not self.apply_fees:
            return 0.0
        if canon_symbol.endswith(".US"):
            return 0.0001  # 1 bp US commission
        if canon_symbol in {"2800.HK", "2828.HK", "3033.HK", "2823.HK", "3081.HK", "3053.HK"}:
            return 0.00015  # 1.5 bps HK stamp-exempt ETF
        return 0.0011  # 11 bps HK stock (stamp duty + levies + commission)

    def _apply_stock_fill(self, rec: ServerOrderRecord, inc_qty: int, fill_px: float) -> None:
        if inc_qty <= 0 or rec.canon_symbol is None:
            return
        canon = rec.canon_symbol
        ccy: CurrencyCode = "HKD" if canon.endswith(".HK") else "USD"
        lag = 2 if ccy == "HKD" else 1
        notional = inc_qty * fill_px
        fee = notional * self._fee_rate_for(canon)
        action = str(rec.order.action).upper()

        if action == "SELL":
            proceeds = notional - fee
            self.cash_balance[ccy] += proceeds
            self.settlement_ledger.append(
                SettlementEntry(
                    settle_day=self.current_day + lag,
                    currency=ccy,
                    amount=proceeds,
                    description=f"SELL {inc_qty} {canon} @ {fill_px:.4f}",
                )
            )
            cur = self.holdings.get(canon)
            old_q = cur.quantity if cur else 0
            new_q = max(0, old_q - inc_qty)
            if new_q == 0:
                self.holdings.pop(canon, None)
            elif cur is not None:
                cur.quantity = new_q
                cur.market_price = fill_px
        else:
            outlay = notional + fee
            self.cash_balance[ccy] -= outlay
            self.settlement_ledger.append(
                SettlementEntry(
                    settle_day=self.current_day + lag,
                    currency=ccy,
                    amount=-outlay,
                    description=f"BUY {inc_qty} {canon} @ {fill_px:.4f}",
                )
            )
            cur = self.holdings.get(canon)
            old_q = cur.quantity if cur else 0
            new_q = old_q + inc_qty
            new_avg = (
                ((old_q * cur.avg_cost) + outlay) / new_q
                if cur is not None and old_q > 0
                else outlay / inc_qty
            )
            self.set_position(canon, new_q, new_avg, fill_px)

        prev_filled = rec.filled
        new_filled = prev_filled + inc_qty
        rec.avg_fill_price = (
            ((prev_filled * rec.avg_fill_price) + (inc_qty * fill_px)) / new_filled
            if new_filled > 0
            else 0.0
        )
        rec.filled = new_filled
        rec.last_fill_price = fill_px
        rec.remaining = max(0.0, float(rec.order.totalQuantity) - new_filled)
        exec_id = f"EX-{self._next_exec_id:06d}"
        self._next_exec_id += 1
        rec.fills.append(
            FillExecution(exec_id=exec_id, shares=float(inc_qty), price=fill_px, time_sec=self.clock_sec)
        )
        if rec.remaining <= 0.0:
            rec.status = "Filled"
            rec.log.append(TradeLogEntry(self.clock_sec, "Filled", f"Filled {inc_qty} @ {fill_px}"))
        else:
            rec.status = "Submitted"
            rec.log.append(
                TradeLogEntry(self.clock_sec, "Submitted", f"Partial fill {inc_qty} @ {fill_px}")
            )

    def _apply_fx_fill(self, rec: ServerOrderRecord, usd_qty: float, fill_rate: float) -> None:
        action = str(rec.order.action).upper()
        exch = str(getattr(rec.contract, "exchange", "IDEALPRO")).upper()
        hkd_amt = usd_qty * fill_rate
        # Spot USD.HKD settles T+2 on both IDEALPRO and FXCONV
        if action == "SELL":
            self.cash_balance["USD"] -= usd_qty
            self.cash_balance["HKD"] += hkd_amt
            self.settlement_ledger.append(
                SettlementEntry(self.current_day + 2, "USD", -usd_qty, f"FX SELL USD.HKD@{exch}")
            )
            self.settlement_ledger.append(
                SettlementEntry(self.current_day + 2, "HKD", hkd_amt, f"FX BUY HKD@{exch}")
            )
            signed_usd = -usd_qty
        else:
            self.cash_balance["USD"] += usd_qty
            self.cash_balance["HKD"] -= hkd_amt
            self.settlement_ledger.append(
                SettlementEntry(self.current_day + 2, "USD", usd_qty, f"FX BUY USD.HKD@{exch}")
            )
            self.settlement_ledger.append(
                SettlementEntry(self.current_day + 2, "HKD", -hkd_amt, f"FX SELL HKD@{exch}")
            )
            signed_usd = usd_qty

        # Critical IBKR distinction: IDEALPRO opens a Virtual FX Position; FXCONV does NOT!
        if exch == "IDEALPRO":
            self.virtual_fx_usd_position += signed_usd
            self.virtual_fx_avg_rate = fill_rate

        rec.filled = usd_qty
        rec.remaining = 0.0
        rec.avg_fill_price = fill_rate
        rec.last_fill_price = fill_rate
        rec.status = "Filled"
        exec_id = f"FX-{self._next_exec_id:06d}"
        self._next_exec_id += 1
        rec.fills.append(
            FillExecution(exec_id=exec_id, shares=usd_qty, price=fill_rate, time_sec=self.clock_sec)
        )
        rec.log.append(TradeLogEntry(self.clock_sec, "Filled", f"FX filled @ {fill_rate:.4f}"))

    def _evaluate_order(self, rec: ServerOrderRecord) -> None:
        if rec.status in OrderStatus.DoneStates:
            return
        sec_type = str(getattr(rec.contract, "secType", "STK") or "STK").upper()
        action = str(rec.order.action).upper()
        ord_type = str(rec.order.orderType).upper()
        lmt = float(getattr(rec.order, "lmtPrice", 0.0) or 0.0)

        if sec_type == "CASH":
            if ord_type == "MKT":
                rate = self.usdhkd_bid if action == "SELL" else self.usdhkd_ask
                self._apply_fx_fill(rec, float(rec.order.totalQuantity), rate)
            elif action == "SELL" and lmt <= self.usdhkd_bid:
                self._apply_fx_fill(rec, float(rec.order.totalQuantity), self.usdhkd_bid)
            elif action == "BUY" and lmt >= self.usdhkd_ask:
                self._apply_fx_fill(rec, float(rec.order.totalQuantity), self.usdhkd_ask)
            self._sync_record_to_clients(rec)
            return

        canon = rec.canon_symbol
        if canon is None:
            return
        q = self.quotes.get(canon)
        if q is None or q.bid <= 0.0 or q.ask <= 0.0 or q.ask < q.bid:
            return

        script = self.scripts.get(canon)
        eval_idx = rec.eval_count
        rec.eval_count += 1

        # Determine whether order price is marketable under server fill mode
        mid = 0.5 * (q.bid + q.ask)
        exec_px: float | None = None
        if ord_type == "MKT":
            exec_px = q.ask if action == "BUY" else q.bid
        elif action == "BUY":
            if lmt >= q.ask:
                exec_px = q.ask
            elif self.fill_mode == "MID_OR_BETTER" and lmt >= mid:
                exec_px = lmt
        else:
            if lmt <= q.bid:
                exec_px = q.bid
            elif self.fill_mode == "MID_OR_BETTER" and lmt <= mid:
                exec_px = lmt

        if script is not None and script.fill_price_override is not None and exec_px is not None:
            exec_px = script.fill_price_override

        if exec_px is None:
            self._sync_record_to_clients(rec)
            return

        total_qty = round(float(rec.order.totalQuantity))
        allowed_cum = total_qty
        if script is not None and script.max_cum_fill_by_eval is not None:
            # Find highest eval_idx <= current eval_idx in max_cum_fill_by_eval, else 0
            matching_keys = [k for k in script.max_cum_fill_by_eval if k <= eval_idx]
            allowed_cum = (
                min(total_qty, script.max_cum_fill_by_eval[max(matching_keys)])
                if matching_keys
                else 0
            )

        inc = max(0, allowed_cum - round(rec.filled))
        if inc > 0:
            self._apply_stock_fill(rec, inc, exec_px)
        self._sync_record_to_clients(rec)

    def _sync_record_to_clients(self, rec: ServerOrderRecord) -> None:
        for client in self._clients.values():
            client._on_server_order_update(rec)

    def _compute_net_liquidation_base(self) -> float:
        rate = self.usdhkd_mid
        usd_val = self.cash_balance["USD"] + (self.cash_balance["HKD"] / rate)
        for h in self.holdings.values():
            q = self.quotes.get(h.canon_symbol)
            px = 0.5 * (q.bid + q.ask) if (q and q.bid > 0 and q.ask >= q.bid) else h.market_price
            mv = h.quantity * px
            usd_val += mv if h.currency == "USD" else (mv / rate)
        return usd_val if self.base_currency == "USD" else (usd_val * rate)

    def build_account_values(self) -> list[AccountValue]:
        """Emit TWS `accountValues()` faithfully:
        - `CashBalance` & `SettledCash` emitted per currency (`USD`, `HKD`) AND `BASE`.
        - `AvailableFunds` & `TotalCashValue` emitted ONLY in `self.base_currency` (and `BASE`),
          never for non-base currencies!
        """
        usd_cb = self.cash_balance["USD"]
        hkd_cb = self.cash_balance["HKD"]
        usd_sc = self.settled_cash("USD")
        hkd_sc = self.settled_cash("HKD")
        rate = self.usdhkd_mid
        total_cash_base = (
            usd_cb + (hkd_cb / rate)
            if self.base_currency == "USD"
            else (usd_cb * rate) + hkd_cb
        )
        # Approximate Initial Margin at 25% of long stock value for realistic AvailableFunds
        stock_mv_usd = sum(
            h.quantity * h.market_price * (1.0 if h.currency == "USD" else 1.0 / rate)
            for h in self.holdings.values()
        )
        init_margin_base = 0.25 * (
            stock_mv_usd if self.base_currency == "USD" else stock_mv_usd * rate
        )
        avail_base = max(0.0, self._compute_net_liquidation_base() - init_margin_base)

        if self.emit_ledger_cash_tags:
            d0 = 20261116 + self.current_day
            d1 = d0 + 1
            return [
                AccountValue("$LEDGER-CashBalance", f"{usd_cb:.6f}", "USD", account=self.account_id),
                AccountValue(
                    "$LEDGER-TotalCashBalance", f"{usd_cb:.6f}", "USD", account=self.account_id
                ),
                AccountValue(
                    "SettledCashByDate",
                    f"{d0}:{usd_sc:.8f};{d1}:{usd_cb:.8f}",
                    "USD",
                    account=self.account_id,
                ),
                AccountValue("$LEDGER-CashBalance", f"{hkd_cb:.6f}", "HKD", account=self.account_id),
                AccountValue(
                    "$LEDGER-TotalCashBalance", f"{hkd_cb:.6f}", "HKD", account=self.account_id
                ),
                AccountValue(
                    "SettledCashByDate",
                    f"{d0}:{hkd_sc:.8f};{d1}:{hkd_cb:.8f}",
                    "HKD",
                    account=self.account_id,
                ),
                AccountValue(
                    "TotalCashValue",
                    f"{total_cash_base:.2f}",
                    self.base_currency,
                    account=self.account_id,
                ),
                AccountValue(
                    "AvailableFunds",
                    f"{avail_base:.2f}",
                    self.base_currency,
                    account=self.account_id,
                ),
                AccountValue(
                    "$LEDGER-CashBalance",
                    f"{total_cash_base:.2f}",
                    "BASE",
                    account=self.account_id,
                ),
            ]

        return [
            AccountValue("CashBalance", f"{usd_cb:.2f}", "USD", account=self.account_id),
            AccountValue("SettledCash", f"{usd_sc:.2f}", "USD", account=self.account_id),
            AccountValue("CashBalance", f"{hkd_cb:.2f}", "HKD", account=self.account_id),
            AccountValue("SettledCash", f"{hkd_sc:.2f}", "HKD", account=self.account_id),
            AccountValue(
                "TotalCashValue",
                f"{total_cash_base:.2f}",
                self.base_currency,
                account=self.account_id,
            ),
            AccountValue(
                "AvailableFunds",
                f"{avail_base:.2f}",
                self.base_currency,
                account=self.account_id,
            ),
            AccountValue("CashBalance", f"{total_cash_base:.2f}", "BASE", account=self.account_id),
        ]

    def build_portfolio_items(self) -> list[PortfolioItem]:
        items: list[PortfolioItem] = []
        for h in sorted(self.holdings.values(), key=lambda x: x.canon_symbol):
            if h.quantity == 0:
                continue
            q = self.quotes.get(h.canon_symbol)
            px = 0.5 * (q.bid + q.ask) if (q and q.bid > 0 and q.ask >= q.bid) else h.market_price
            mv = h.quantity * px
            items.append(
                PortfolioItem(
                    contract=Stock(
                        symbol=h.ibkr_symbol, exchange=h.exchange, currency=h.currency
                    ),
                    position=float(h.quantity),
                    marketPrice=px,
                    marketValue=mv,
                    averageCost=h.avg_cost,
                    unrealizedPNL=(px - h.avg_cost) * h.quantity,
                    account=self.account_id,
                )
            )
        if abs(self.virtual_fx_usd_position) > 1e-6:
            # IDEALPRO Virtual FX Position appears in portfolio() as CASH USD.HKD!
            items.append(
                PortfolioItem(
                    contract=Forex("USDHKD", exchange="IDEALPRO"),
                    position=self.virtual_fx_usd_position,
                    marketPrice=self.usdhkd_mid,
                    marketValue=self.virtual_fx_usd_position * self.usdhkd_mid,
                    averageCost=self.virtual_fx_avg_rate,
                    account=self.account_id,
                )
            )
        return items


class FakeIB:
    """Drop-in `ib_async.IB` socket client connected to a shared `FakeTwsServer`."""

    def __init__(self, server: FakeTwsServer) -> None:
        self._server = server
        self.clientId: int = 0
        self.connected: bool = False
        self.market_data_type: int = 1
        # Keyed by (client_id, order_id, perm_id) just like ib_async Wrapper.trades
        self._trades: dict[tuple[int, int, int], Trade] = {}
        self._trades_by_order_id: dict[int, Trade] = {}
        self.req_open_orders_calls: int = 0
        self.req_all_open_orders_calls: int = 0
        self.sleep_calls: list[float] = []
        self.errorEvent: FakeEvent = FakeEvent()

    def _require_connected(self) -> None:
        if not self.connected:
            raise ConnectionError("Socket disconnect: FakeIB is not connected to FakeTwsServer")

    def connect(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        clientId: int = 1,
        timeout: float = 15.0,
        account: str = "",
    ) -> FakeIB:
        _ = (host, port, timeout, account)
        if clientId in self._server._clients and self._server._clients[clientId] is not self:
            raise ConnectionError(f"Error 326: Unable to connect as the client id {clientId} is in use")
        self.clientId = clientId
        self.connected = True
        self._server._clients[clientId] = self
        # On connect, TWS automatically pushes open orders belonging strictly to this clientId
        for rec in self._server.orders:
            if rec.client_id == self.clientId and rec.status not in OrderStatus.DoneStates:
                self._on_server_order_update(rec)
        return self

    def disconnect(self) -> None:
        if self.connected:
            self._server._clients.pop(self.clientId, None)
            self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def reqMarketDataType(self, marketDataType: int) -> None:
        self._require_connected()
        if marketDataType not in (1, 2, 3, 4):
            self._server._emit_error(
                0, 321, f"Error validating request: invalid marketDataType {marketDataType}", self.clientId
            )
            return
        self.market_data_type = int(marketDataType)

    def _on_server_order_update(self, rec: ServerOrderRecord) -> Trade:
        key = (rec.client_id, rec.order_id, rec.perm_id)
        existing = self._trades.get(key)
        if existing is None and rec.client_id == self.clientId and rec.order_id in self._trades_by_order_id:
            existing = self._trades_by_order_id[rec.order_id]
            self._trades[key] = existing

        if existing is None:
            st = OrderStatus(
                orderId=rec.order_id,
                status=rec.status,
                filled=rec.filled,
                remaining=rec.remaining,
                avgFillPrice=rec.avg_fill_price,
                permId=rec.perm_id,
                clientId=rec.client_id,
                lastFillPrice=rec.last_fill_price,
            )
            tr = Trade(
                contract=copy.copy(rec.contract),
                order=copy.copy(rec.order),
                orderStatus=st,
                fills=list(rec.fills),
                log=list(rec.log),
            )
            self._trades[key] = tr
            if rec.client_id == self.clientId and rec.order_id > 0:
                self._trades_by_order_id[rec.order_id] = tr
            return tr

        existing.orderStatus.status = rec.status
        existing.orderStatus.filled = rec.filled
        existing.orderStatus.remaining = rec.remaining
        existing.orderStatus.avgFillPrice = rec.avg_fill_price
        existing.orderStatus.permId = rec.perm_id
        existing.orderStatus.lastFillPrice = rec.last_fill_price
        existing.fills = list(rec.fills)
        existing.log = list(rec.log)
        return existing

    def sleep(self, sec: float = 0.0) -> bool:
        self._require_connected()
        self.sleep_calls.append(sec)
        self._server.clock_sec += max(0.0, sec)

        for hook in list(self._server.sleep_hooks):
            hook(self._server, sec)

        # Evaluate active orders on server during event-loop pump
        for rec in list(self._server.orders):
            if rec.status not in OrderStatus.DoneStates:
                script = self._server.scripts.get(rec.canon_symbol or "")
                if (
                    script is not None
                    and script.disconnect_on_poll
                    and rec.client_id == self.clientId
                    and sec > 0.6
                ):
                    script.disconnect_on_poll = False
                    self.disconnect()
                    raise ConnectionError("Simulated TWS socket drop during poll_order sleep")
                self._server._evaluate_order(rec)
        return True

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        self._require_connected()
        qualified: list[Any] = []
        for c in contracts:
            if self._server._qualify_one(c, client_id=self.clientId):
                qualified.append(c)
        return qualified

    def reqTickers(self, *contracts: Any) -> list[Ticker]:
        self._require_connected()
        out: list[Ticker] = []
        for c in contracts:
            sec_type = str(getattr(c, "secType", "STK") or "STK").upper()
            if sec_type == "CASH":
                out.append(
                    Ticker(
                        contract=c,
                        bid=self._server.usdhkd_bid,
                        ask=self._server.usdhkd_ask,
                        last=self._server.usdhkd_mid,
                        close=self._server.usdhkd_mid,
                    )
                )
                continue

            canon = self._server._resolve_stock_canon(c, client_id=self.clientId, req_id=0)
            # Simulate real asyncio event-loop pumping: while awaiting reqTickers(), an active
            # order with `async_fill_on_req_tickers=True` receives its fill callback!
            if canon is not None:
                script = self._server.scripts.get(canon)
                if script is not None and script.async_fill_on_req_tickers:
                    for rec in self._server.orders:
                        if (
                            rec.canon_symbol == canon
                            and rec.status not in OrderStatus.DoneStates
                        ):
                            q_fill = self._server.quotes.get(canon)
                            px = (
                                float(rec.order.lmtPrice)
                                if q_fill is None
                                else (
                                    min(float(rec.order.lmtPrice), q_fill.ask)
                                    if str(rec.order.action).upper() == "BUY"
                                    else max(float(rec.order.lmtPrice), q_fill.bid)
                                )
                            )
                            rem_q = round(rec.remaining)
                            if rem_q > 0:
                                self._server._apply_stock_fill(rec, rem_q, px)
                                self._server._sync_record_to_clients(rec)
                if script is not None and script.async_cancel_on_req_tickers:
                    for rec in self._server.orders:
                        if (
                            rec.canon_symbol == canon
                            and rec.status not in OrderStatus.DoneStates
                        ):
                            rec.status = "Cancelled"
                            rec.log.append(
                                TradeLogEntry(
                                    self._server.clock_sec,
                                    "Cancelled",
                                    "Async server cancel during reqTickers",
                                )
                            )
                            self._server._sync_record_to_clients(rec)

            q = self._server.quotes.get(canon or "")
            if self._server.has_live_equity_subscription:
                if q is None:
                    out.append(
                        Ticker(contract=c, bid=0.0, ask=0.0, last=0.0, close=0.0, marketDataType=1)
                    )
                else:
                    out.append(
                        Ticker(
                            contract=c,
                            bid=q.bid,
                            ask=q.ask,
                            last=q.last,
                            close=q.close,
                            marketDataType=1,
                        )
                    )
                continue

            # Unsubscribed account (`has_live_equity_subscription == False`):
            if self.market_data_type in (1, 2):
                self._server._emit_error(
                    0,
                    10089,
                    f"Requested market data requires additional subscription for API. ({canon or getattr(c, 'symbol', '')})",
                    self.clientId,
                )
                out.append(
                    Ticker(
                        contract=c,
                        bid=-1.0,
                        ask=-1.0,
                        last=-1.0,
                        close=q.close if q is not None else -1.0,
                        marketDataType=self.market_data_type,
                    )
                )
                continue

            dq = self._server.delayed_quotes.get(canon or "") or q
            if dq is None:
                out.append(
                    Ticker(
                        contract=c,
                        bid=-1.0,
                        ask=-1.0,
                        last=-1.0,
                        close=-1.0,
                        delayedBid=-1.0,
                        delayedAsk=-1.0,
                        delayedLast=-1.0,
                        delayedClose=-1.0,
                        marketDataType=self.market_data_type,
                    )
                )
            else:
                out.append(
                    Ticker(
                        contract=c,
                        bid=-1.0,
                        ask=-1.0,
                        last=-1.0,
                        close=-1.0,
                        delayedBid=dq.bid,
                        delayedAsk=dq.ask,
                        delayedLast=dq.last,
                        delayedClose=dq.close,
                        marketDataType=self.market_data_type,
                    )
                )
        return out

    def portfolio(self, account: str = "") -> list[PortfolioItem]:
        self._require_connected()
        items = self._server.build_portfolio_items()
        if account:
            return [it for it in items if it.account == account]
        return items

    def positions(self, account: str = "") -> list[Position]:
        self._require_connected()
        out: list[Position] = []
        for it in self.portfolio(account):
            out.append(
                Position(
                    account=it.account,
                    contract=it.contract,
                    position=it.position,
                    avgCost=it.averageCost,
                )
            )
        return out

    def accountValues(self, account: str = "") -> list[AccountValue]:
        self._require_connected()
        vals = self._server.build_account_values()
        if account:
            return [v for v in vals if v.account == account]
        return vals

    def reqOpenOrders(self) -> list[Trade]:
        """Official TWS `reqOpenOrders()` semantics:
        - Returns ONLY open orders belonging to `self.clientId`.
        - Special case `clientId == 0`: also binds manual TWS GUI orders (`client_id == -1`)
          to `client_id = 0` with newly assigned API `orderId`s, still omitting non-zero API
          `clientId`s (`26`, `27`, ...).
        """
        self._require_connected()
        self.req_open_orders_calls += 1
        matched: list[Trade] = []
        for rec in self._server.orders:
            if rec.status in OrderStatus.DoneStates:
                continue
            if self.clientId == 0 and rec.client_id == -1:
                rec.client_id = 0
                rec.order_id = self._server._alloc_order_id(0)
                rec.order.clientId = 0
                rec.order.orderId = rec.order_id
            if rec.client_id == self.clientId:
                matched.append(self._on_server_order_update(rec))
        return matched

    def reqAllOpenOrders(self) -> list[Trade]:
        """Official TWS `reqAllOpenOrders()` semantics:
        Streams open orders across ALL `clientId`s and manual TWS GUI orders (`client_id == -1`),
        populating `self._trades` so `openTrades()` sees the full account-wide active book.
        """
        self._require_connected()
        self.req_all_open_orders_calls += 1
        all_open: list[Trade] = []
        for rec in self._server.orders:
            if rec.status not in OrderStatus.DoneStates:
                all_open.append(self._on_server_order_update(rec))
        return all_open

    def openTrades(self) -> list[Trade]:
        """Exact `ib_async.IB.openTrades()` semantics: all cached trades not in `DoneStates`."""
        self._require_connected()
        return [
            tr
            for tr in self._trades.values()
            if tr.orderStatus.status not in OrderStatus.DoneStates
        ]

    def placeOrder(self, contract: Any, order: Any) -> Trade:
        self._require_connected()
        oid = int(getattr(order, "orderId", 0) or 0)

        # Case A: In-place modification of an existing order on this clientId
        if oid > 0 and oid in self._trades_by_order_id:
            existing_trade = self._trades_by_order_id[oid]
            # Exact `ib_async` client-side guard (`ib_async/ib.py:placeOrder`):
            assert existing_trade.orderStatus.status not in OrderStatus.DoneStates, (
                f"Cannot modify Trade {oid} already in DoneStates: "
                f"{existing_trade.orderStatus.status}"
            )
            rec = next(
                (
                    r
                    for r in self._server.orders
                    if r.client_id == self.clientId and r.order_id == oid
                ),
                None,
            )
            if rec is None:
                self._server._emit_error(oid, 135, f"Can't find order with id = {oid}", self.clientId)
                return existing_trade

            script = self._server.scripts.get(rec.canon_symbol or "")
            # Server-side wire race: order fills at exchange at its PRIOR limit right before modify arrives!
            if (
                script is not None
                and script.wire_fill_race_on_modify
                and rec.status not in OrderStatus.DoneStates
            ):
                rem_q = round(rec.remaining)
                if rem_q > 0:
                    # Fill at prior working limit BEFORE order modification is applied on server!
                    prior_px = float(rec.order.lmtPrice)
                    self._server._apply_stock_fill(rec, rem_q, prior_px)
                self._server._emit_error(
                    oid, 104, "Can't modify a filled order", self.clientId
                )
                return self._on_server_order_update(rec)

            if rec.status in OrderStatus.DoneStates:
                code = 104 if rec.status == "Filled" else 201
                msg = (
                    "Can't modify a filled order"
                    if rec.status == "Filled"
                    else f"Order rejected - Cannot modify order in state {rec.status}"
                )
                self._server._emit_error(oid, code, msg, self.clientId)
                return self._on_server_order_update(rec)

            rec.order = copy.copy(order)
            if self._server._validate_order(rec, is_modify=True):
                rec.log.append(
                    TradeLogEntry(
                        self._server.clock_sec,
                        rec.status,
                        f"Modified lmtPrice={getattr(order, 'lmtPrice', 0.0)}",
                    )
                )
                self._server._evaluate_order(rec)
            else:
                self._server._sync_record_to_clients(rec)
            return existing_trade

        # Case B: Fresh order submission
        if oid == 0:
            oid = self._server._alloc_order_id(self.clientId)
            order.orderId = oid
        order.clientId = self.clientId
        perm_id = self._server._alloc_perm_id()
        order.permId = perm_id

        sec_type = str(getattr(contract, "secType", "STK") or "STK").upper()
        canon_sym = (
            None
            if sec_type == "CASH"
            else self._server._resolve_stock_canon(contract, client_id=self.clientId, req_id=oid)
        )
        rec = ServerOrderRecord(
            perm_id=perm_id,
            client_id=self.clientId,
            order_id=oid,
            account=str(getattr(order, "account", "") or self._server.account_id),
            contract=copy.copy(contract),
            canon_symbol=canon_sym,
            order=copy.copy(order),
            status="PendingSubmit",
            filled=0.0,
            remaining=float(order.totalQuantity),
            log=[TradeLogEntry(self._server.clock_sec, "PendingSubmit", "Order transmitted")],
        )
        self._server.orders.append(rec)

        if self._server._validate_order(rec, is_modify=False):
            init_active = (
                "PreSubmitted"
                if getattr(order, "algoStrategy", "") == "Adaptive"
                else "Submitted"
            )
            rec.status = init_active
            rec.log.append(TradeLogEntry(self._server.clock_sec, init_active, "Accepted by TWS"))
            if init_active == "PreSubmitted":
                rec.status = "Submitted"
            self._server._evaluate_order(rec)

        tr = self._on_server_order_update(rec)
        if canon_sym is not None:
            script = self._server.scripts.get(canon_sym)
            if script is not None and script.disconnect_after_place:
                script.disconnect_after_place = False
                self.disconnect()
                raise ConnectionError("Simulated TWS socket drop immediately after placeOrder")
        return tr

    def cancelOrder(self, order: Any) -> Trade | None:
        self._require_connected()
        oid = int(getattr(order, "orderId", 0) or 0)
        rec = next(
            (
                r
                for r in self._server.orders
                if r.client_id == self.clientId and r.order_id == oid
            ),
            None,
        )
        if rec is None:
            self._server._emit_error(oid, 135, f"Can't find order with id = {oid}", self.clientId)
            return None
        if rec.status == "Filled":
            self._server._emit_error(
                oid,
                10148,
                f"OrderId {oid} that needs to be cancelled can not be cancelled, state: Filled.",
                self.clientId,
            )
            return self._on_server_order_update(rec)
        if rec.status not in OrderStatus.DoneStates:
            rec.status = (
                "ApiCancelled" if rec.status in ("PendingSubmit", "ApiPending") else "Cancelled"
            )
            rec.log.append(TradeLogEntry(self._server.clock_sec, rec.status, "Cancelled by API"))
            self._server._sync_record_to_clients(rec)
        return self._trades_by_order_id.get(oid)


def build_fake_ib_async_module(server: FakeTwsServer) -> types.ModuleType:
    """Create a drop-in `sys.modules['ib_async']` module bound to `server`."""
    mod = types.ModuleType("ib_async")
    mod.IB = lambda: FakeIB(server)  # type: ignore[attr-defined]
    mod.Stock = Stock  # type: ignore[attr-defined]
    mod.Forex = Forex  # type: ignore[attr-defined]
    mod.TagValue = TagValue  # type: ignore[attr-defined]
    mod.LimitOrder = LimitOrder  # type: ignore[attr-defined]
    mod.MarketOrder = MarketOrder  # type: ignore[attr-defined]
    mod.OrderStatus = OrderStatus  # type: ignore[attr-defined]
    mod.Trade = Trade  # type: ignore[attr-defined]
    mod.Position = Position  # type: ignore[attr-defined]
    mod.PortfolioItem = PortfolioItem  # type: ignore[attr-defined]
    mod.AccountValue = AccountValue  # type: ignore[attr-defined]
    mod.Ticker = Ticker  # type: ignore[attr-defined]
    return mod
