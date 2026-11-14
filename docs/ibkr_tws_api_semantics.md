# IBKR TWS API & `ib_async` Execution Semantics Reference

This document specifies the wire-level and client-wrapper (`ib_async` / `ibapi`) execution semantics of Interactive Brokers Trader Workstation (TWS) and IB Gateway governing `fundconnect-ibkr-bot`. All rules below are grounded in official IBKR TWS API documentation and `ib_async` (`ib-api-reloaded/ib_async`) wrapper behavior for dual-market US (`SMART`, `USD`) and Hong Kong (`SEHK`, `HKD`) portfolio rebalancing.

---

## 1. `OrderStatus` Lifecycle, Partial Fills, and Rejection States

In the TWS socket protocol (`EWrapper.orderStatus`) and `ib_async.OrderStatus`, every submitted `Trade` progresses through explicit state sets rather than synchronous request/response completion.

### 1.1 State Partition in `ib_async`

`ib_async.OrderStatus` partitions lifecycle strings into active and terminal sets:

- **`OrderStatus.ActiveStates`**:
  `frozenset({'PendingSubmit', 'ApiPending', 'PreSubmitted', 'Submitted'})`
- **`OrderStatus.DoneStates`**:
  `frozenset({'Filled', 'Cancelled', 'ApiCancelled', 'Inactive'})` (note: older `ib_insync` releases omitted `'Inactive'` from `DoneStates`, whereas modern `ib_async` treats `'Inactive'` as terminal alongside `'Filled'`, `'Cancelled'`, and `'ApiCancelled'`; `'PendingCancel'` is **not** in `DoneStates`).

### 1.2 State Definitions and Transitions

| `orderStatus.status` | Terminal? | Wire Meaning & Transition Conditions |
| :--- | :---: | :--- |
| `ApiPending` | No | Local `ib_async` state immediately after `IB.placeOrder()` transmits the socket frame, prior to any `openOrder` / `orderStatus` callback from TWS. |
| `PendingSubmit` | No | TWS received the order instruction from the API client and is performing pre-trade risk/margin validation or awaiting confirmation from the routing destination (also seen when submitting while an exchange is closed). |
| `PreSubmitted` | No | Accepted by IBKR servers and held prior to active book display at the destination—typical for `Adaptive` IBAlgo orders initializing, simulated/triggered order types, or pre-open auction queues. |
| `Submitted` | No | Working live at the exchange or IBKR SmartRouter/algo engine. **Partial fills do not change `status` to a separate string**: during partial execution, `status` remains `'Submitted'` (or `'PreSubmitted'`) while `filled > 0`, `remaining = totalQuantity - filled > 0`, and `avgFillPrice` reflects the cumulative volume-weighted execution price. |
| `PendingCancel` | No | API client called `IB.cancelOrder(order)`, and TWS forwarded the cancel request to the venue, but confirmation has not yet arrived. **Executions can still arrive while in `PendingCancel`**. If the order fills completely before the venue processes the cancel, the order transitions `PendingCancel -> Filled` and TWS emits **Error `10148`** (*"OrderId ... that needs to be cancelled can not be cancelled, state: Filled"*). |
| `Filled` | Yes | Complete execution (`filled == totalQuantity` and `remaining == 0`). No further modifications or cancellations are valid. |
| `ApiCancelled` | Yes | Cancelled by the API client after transmission before IBKR/venue acknowledged working status (`PendingSubmit -> ApiCancelled`). |
| `Cancelled` | Yes | Remainder confirmed cancelled by IBKR or the exchange (`remaining` becomes cancelled; if preceded by partial fills, `filled > 0` holds the executed shares at `avgFillPrice`). |
| `Inactive` | Yes | Order received by TWS (`orderId` assigned) but **not working** due to system/exchange rejection, compliance/margin block, or parameter error. An `Inactive` order executes zero additional shares unless manually repaired in TWS. |

### 1.3 Key IBKR Error Codes Relevant to US/SEHK Execution

- **`Error 110`** — *"The price does not conform to the minimum price variation for this contract."*
  - Triggered when `lmtPrice` (or `auxPrice`) violates the venue's tick schedule—either from IEEE-754 binary float artifacts (e.g., `81.05000000000001`) or from using an invalid price band step on HKEX's Second Schedule Spread Table (Part A equities, Part B `3053.HK` `0.05` tick, Part D equity/gold ETPs).
  - On initial `placeOrder`: TWS transitions `orderStatus.status` to `'Inactive'` (zero shares working).
  - On in-place `placeOrder` modification: TWS emits `Error 110` and rejects the price update; depending on venue/algo state, either the prior working order remains active at its previous limit price or the order becomes `'Inactive'`.
- **`Error 200`** — *"No security definition has been found for the request."*
  - Triggered when contract fields fail to resolve a unique IBKR `conId`—most notably on SEHK if leading zeros are retained (`Stock('0003', 'SEHK', 'HKD')` fails with `Error 200`, whereas `Stock('3', 'SEHK', 'HKD')` resolves to Hong Kong & China Gas). `IB.qualifyContracts()` logs Error 200 and leaves `contract.conId == 0`.
- **`Error 201`** — *"Order rejected - Reason: ..."*
  - Triggered by pre-trade cash/margin checks (e.g., insufficient settled cash on a Cash account, insufficient initial margin / `AvailableFunds` on a Margin account, missing `order.account` in multi-account setups, short-sale restriction, or exchange fat-finger price caps). Transitions `orderStatus.status` to `'Inactive'` (or `'Cancelled'`).

---

## 2. In-Place Order Modification (`placeOrder`) and Fill/Terminal Races

TWS API modifies a live order by calling `placeOrder(contract, order)` with the **same `orderId`** (and matching `clientId`) and updated order attributes such as `order.lmtPrice`. Two distinct race hazards arise in event-driven wrappers like `ib_async`:

### 2.1 Client-Side `ib_async` Wrapper Assertion Hazard

Inside `ib_async.IB.placeOrder(contract, order)`, the wrapper looks up `(clientId, orderId)` in `self.wrapper.trades`:

```python
trade = self.wrapper.trades.get(key)
if trade:
    # Modification of an existing Trade:
    assert trade.orderStatus.status not in OrderStatus.DoneStates
```

Because synchronous `ib_async` helpers—including `ib.sleep()`, `ib.qualifyContracts()`, `ib.reqTickers()`, `ib.reqOpenOrders()`, and `ib.reqAllOpenOrders()`—all pump the underlying `asyncio` socket loop (`ib.run(...)`), **any blocking API call between polling an order and modifying it can process incoming `execDetails` / `orderStatus` frames**.

Concretely, in a walk-limit loop:
1. `poll_order()` observes `status == 'Submitted'` (`WORKING` or `PARTIAL`).
2. `get_quotes()` invokes `ib.qualifyContracts()` and `ib.reqTickers()`. While awaiting ticker snapshots, TWS delivers a fill or rejection packet setting `trade.orderStatus.status = 'Filled'` (or `'Cancelled'` / `'Inactive'`).
3. If `modify_order_limit()` then mutates `trade.order.lmtPrice = float(new_limit_price)` and calls `ib.placeOrder(trade.contract, trade.order)` without re-checking `trade.isDone()` / `OrderStatus.DoneStates`:
   - `ib_async` raises an unhandled **`AssertionError`** inside `placeOrder()`, aborting the execution loop mid-batch before state persistence completes.
   - Mutating `trade.order.lmtPrice` *before* checking terminal status also overwrites the `Trade` object's recorded `lmtPrice` even though no modification was accepted.

### 2.2 Server-Side Wire Race (`Error 104`)

Even when `trade.orderStatus.status` is still `'Submitted'` locally at the instant `ib.placeOrder()` writes the modify frame to the socket, the exchange matching engine can fill the remaining shares before TWS processes the modification:

- TWS emits **`Error 104: "Can't modify a filled order."`** (or `Error 103` / `Error 135` / `Error 201` if already cancelled/inactive on the server) via `EWrapper.error`, followed by (or alongside) `orderStatus` `'Filled'` with `filled == totalQuantity` and `avgFillPrice` equal to the **actual execution price(s)** prior to modification.
- Safe callers must:
  1. Check `trade.orderStatus.status in OrderStatus.DoneStates` (or `filled >= totalQuantity`) immediately before calling `ib.placeOrder()` for a modification, returning the terminal `FillReport` without transmitting a modify frame or incrementing `reprice_count`.
  2. Treat server-side `Error 104` during modification as a benign race won by the fill—preserving actual `filled` and `avgFillPrice` from `trade.orderStatus`.

---

## 3. Open Order Visibility and `clientId` Scoping

TWS partitions API order ownership by `(account, clientId)` (`orderId` sequences are independent per `clientId`):

| Method | Wire Request | Orders Returned / Populated into `ib.openTrades()` | Can Current Client Modify/Cancel? |
| :--- | :--- | :--- | :--- |
| `ib.openTrades()` (alone) | None (local cache) | Only active `Trade` objects already received by *this* `IB` instance during the current socket session (plus auto-sent own-`clientId` orders on connect). | Own `clientId` only |
| `ib.reqOpenOrders()` | `EClient.reqOpenOrders()` | **If `clientId != 0` (e.g., `clientId = 26`)**: returns **only** open orders originally submitted by that exact `clientId`. Open orders from `clientId = 27` or manual TWS GUI (`clientId = 0`) are **completely invisible**.<br>**If `clientId == 0`**: returns `clientId == 0` orders and *binds* manual TWS GUI orders to API `clientId = 0`, **still omitting** open orders placed by non-zero API `clientId`s (`1, 26, ...`). | Own `clientId` (plus bound manual orders if `clientId == 0`) |
| `ib.reqAllOpenOrders()` | `EClient.reqAllOpenOrders()` | Streams `openOrder` + `orderStatus` + `openOrderEnd` for **all open orders across every API `clientId` AND manual TWS desktop orders**, synchronizing them into `ib.openTrades()` and returning `list[Trade]`. Does *not* rebind ownership. | Read-only visibility for foreign `clientId` orders; full modify/cancel on own `clientId` |

**Implication for Crash Recovery & Idempotency**:
When a rebalancer connects with `clientId = 26` (or reconnects under a failover `clientId`, or runs alongside manual operator orders in TWS), calling `ib.reqOpenOrders()` blinds pre-trade reconciliation to any working order created under another `clientId` or TWS GUI. Calling `ib.reqAllOpenOrders()` (and reading the returned trades / synchronized `ib.openTrades()`, filtering out `OrderStatus.DoneStates`) is required for complete account-wide open-order offset reconciliation (`working_by_sym_side`).

---

## 4. Currency Conversion (`USD.HKD`), Account Ledgers, and Settlement (`T+1` vs `T+2`)

### 4.1 `IDEALPRO` vs `FXCONV` Routing for `USD.HKD`

In `ib_async`, `Forex('USDHKD')` constructs `Contract(secType='CASH', symbol='USD', currency='HKD', exchange='IDEALPRO')`. IBKR distinguishes two routing destinations for `CASH` contracts:

1. **Market Data Quotes (`IDEALPRO`)**:
   - `qualifyContracts(Forex('USDHKD'))` and `reqTickers(Forex('USDHKD'))` must use `exchange='IDEALPRO'` to query interbank `USD.HKD` spot bid/ask quotes.
2. **Order Execution (`IDEALPRO` vs `FXCONV`)**:
   - **`exchange='IDEALPRO'`** (default on `Forex('USDHKD')`): Routes as a **speculative / institutional spot FX trade**, which **opens or adjusts a Virtual FX Position** in TWS's *FX Portfolio* panel (tracking synthetic position size, average rate, and unrealized P&L separately from physical cash). In addition, `IDEALPRO` enforces a standard minimum order size of **`25,000 USD`**; orders `< 25,000 USD` emit Warning `399` and route internally as retail odd lots (`IDEAL`) at wider spreads.
   - **`exchange='FXCONV'`** (`Forex('USDHKD', exchange='FXCONV')`): Routes explicitly as a **pure physical currency conversion** to fund cross-border securities. It updates multi-currency cash balances identically to `IDEALPRO` (`T+2` value date) **without** creating a Virtual FX Position in TWS's FX Portfolio.

### 4.2 Settlement Cycles Across Asset Classes

| Leg / Venue | Settlement Cycle | Value Date Example (Monday Trade `T`) |
| :--- | :---: | :--- |
| US Equities & ETFs (`SGOV.US`, `SHY.US`, `AAPL.US` on `SMART`) | **`T+1`** (SEC Rule 15c6-1) | Tuesday (`T+1`) |
| Spot FX Conversion (`USD.HKD` via `FXCONV` / `IDEALPRO`) | **`T+2`** | Wednesday (`T+2`) |
| Hong Kong Equities & ETFs (`3053.HK`, `2800.HK`, `0003.HK` on `SEHK`) | **`T+2`** (HKEX CCASS) | Wednesday (`T+2`) |

### 4.3 `accountValues()` Cash Tags and Zero-Margin-Interest Conditions

When querying `ib.accountValues()`, TWS reports distinct trade-date, settlement-date, and base-currency margin tags:

- **`CashBalance` (emitted per currency: `currency='USD'`, `currency='HKD'`, plus `'BASE'`)**:
  Trade-dated cash balance (`T+0`). Updates **immediately upon order fill**—selling `SGOV.US` immediately credits `CashBalance(USD)`, and selling `USD.HKD` immediately debits `CashBalance(USD)` and credits `CashBalance(HKD)`.
- **`SettledCash` (emitted per currency: `currency='USD'`, `currency='HKD'`, plus `'BASE'`)**:
  Cash recognized as settled as of the current clearing date.
  - Selling `SGOV.US` on Monday (`T`) increases `CashBalance(USD)` immediately, while `SettledCash(USD)` remains unchanged until Tuesday (`T+1`).
  - Buying US equities on Monday (`T`) in the same `T+1` settlement cycle matches Monday's `SGOV.US` `T+1` sale proceeds on Tuesday (`T+1`), incurring **zero margin loan interest**.
  - Selling `USD.HKD` (`T+2`) on Monday (`T`) converts USD into HKD that settles Wednesday (`T+2`). Executing HKEX `T+2` buys before HKD cash settles (or before matched same-cycle SEHK `T+2` sale proceeds exist), or drawing down unsettled USD across mismatched cycles on a Margin account, creates a negative settled-cash balance for 1+ clearing days and accrues IBKR benchmark margin interest (or triggers `Error 201` on a Cash account).
  - On Week 2+ intra-HK rotations, selling `3053.HK` (`SEHK`, `T+2`) and buying `2800.HK` / HK blue chips (`SEHK`, `T+2`) on the **same session** both settle on `T+2` in CCASS, so realized same-session SEHK sale net proceeds fund same-session SEHK buys with **zero margin interest**, even though `SettledCash(HKD)` reported by TWS during session `T` does not yet include the `T+2` sale proceeds.
- **`AvailableFunds` (emitted ONLY in the account's Base Currency, e.g. `currency='USD'` on a USD-base account or `currency='HKD'` on an HKD-base account)**:
  Equals `EquityWithLoanValue - InitialMargin`. Because TWS never emits per-currency `AvailableFunds` for non-base currencies, per-currency non-borrowed buying power cannot rely on `AvailableFunds(HKD)` being present on a USD-base account; it must enforce `CashBalance` and `SettledCash` per currency (plus realized same-cycle Phase A net sale proceeds, minus any unsettled cross-currency FX reserve).
- **`TotalCashValue` (Base Currency aggregate)**:
  Cross-currency sum of all cash balances converted to Base Currency at spot FX; must **never** be treated as single-currency USD or HKD cash.

---

## 5. Summary Checklist for `fundconnect-ibkr-bot` Formal Modeling & E2E Testing

1. **Terminal-State Guard on Walk-Limit Modify**: Before mutating `trade.order.lmtPrice` or calling `ib.placeOrder` in `modify_order_limit`, check whether `trade.orderStatus.status` has already entered `DoneStates` (`Filled`, `Cancelled`, `ApiCancelled`, `Inactive`) during intervening `get_quotes()` / `ib.sleep()` event-loop turns, and handle server-side `Error 104` gracefully without corrupting `FillReport` or crashing mid-batch.
2. **Cross-`clientId` Open Order Visibility**: Use `reqAllOpenOrders()` rather than `reqOpenOrders()` so open orders submitted by prior sessions with different `clientId`s or manual TWS GUI intervention are visible to `get_open_orders()`.
3. **Rejection & Partial-Fill Persistence Semantics**: Distinguish terminal rejected/cancelled orders (`Inactive` / `Error 110` / `Error 201`, `filled == 0`) from active/completed orders when recording `ExecutionState.completed_intents` and FX conversion status (`convert_usd_to_hkd` must check fill completion rather than treating `Inactive`/`Cancelled` or duplicate `--allow-fx-convert` runs as newly settled conversions).
4. **Physical FX Conversion (`FXCONV`) & Settlement Accounting**: Route `USD.HKD` conversion orders via `exchange='FXCONV'` (while keeping `IDEALPRO` for `USD.HKD` quote discovery) so conversions update `CashBalance` / `T+2` `SettledCash` without spawning Virtual FX Positions in TWS.

---

## References

1. Interactive Brokers TWS API Documentation — *Order Submission & Possible Order States (`PendingSubmit`, `PreSubmitted`, `Submitted`, `PendingCancel`, `Cancelled`, `ApiCancelled`, `Inactive`)*: `https://interactivebrokers.github.io/tws-api/order_submission.html`
2. Interactive Brokers TWS API Documentation — *Modifying Orders & Open Orders (`reqOpenOrders`, `reqAllOpenOrders`, `clientId = 0`)*: `https://interactivebrokers.github.io/tws-api/open_orders.html` and `https://interactivebrokers.github.io/tws-api/modifying_orders.html`
3. Interactive Brokers TWS API Documentation — *Message Codes (`Error 104`, `Error 110`, `Error 200`, `Error 201`, `Warning 399`, `Error 10148`)*: `https://interactivebrokers.github.io/tws-api/message_codes.html`
4. Interactive Brokers Campus / TWS Users' Guide — *FXCONV vs. IDEALPRO Virtual FX Positions & Account Summary Tags (`CashBalance`, `SettledCash`, `AvailableFunds`)*: `https://www.ibkrguides.com/tws/usersguidebook/realtimeactivitymonitoring/fxportfolio.htm` and `https://interactivebrokers.github.io/tws-api/account_summary.html`
5. `ib-api-reloaded/ib_async` Documentation — *`IB.placeOrder`, `IB.openTrades`, `IB.reqAllOpenOrders`, `OrderStatus.ActiveStates` / `OrderStatus.DoneStates`, and `Forex` Contracts*: `https://ib-api-reloaded.github.io/ib_async/api.html`
