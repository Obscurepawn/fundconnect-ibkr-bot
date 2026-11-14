# 01_reasoning — L0_GapSizingAndHkex

## The one concern this level adds
Base rung (`L0`): pins the pure, single-snapshot decision and microstructure state machine of `build_execution_batch` (`policy.py`) and `hkex_rules.py` in isolation before introducing asynchronous TWS socket callbacks (`L1`), multi-day cash settlement (`L2`), or multi-run crash/retry persistence (`L3`).

## Mode
**Faithful transcription (zero fault injection)** — `build_execution_batch` and `hkex_rules` are deterministic, pure functions of a point-in-time portfolio, quote map, and `WeeklyPlan`. Halted/invalid quotes (`!q.is_valid`) and wide spreads (`spread_bps > max_spread_bps`) are modeled directly as input quote regimes in `Init` rather than external faults.

## Concrete vs abstracted
| Modeled concretely | Abstracted / factorized (and why sound) |
|---|---|
| Exact integer-cent (`0.01 HKD = 1` cent) HKEX Spread Table bands covering Part A (`0002.HK`: `[10,20]->0.01`, `(20,50]->0.02`, `(50,100]->0.05`, `(100,200]->0.10`, `(200,500]->0.20`), Part B (`3053.HK` -> `0.05`), and Part D (`2828.HK` -> `(20,100]->0.02`), including band-boundary prices such as `200.05 HKD` (`20005` cents). | Sub-`0.50 HKD` penny bands (`0.001` / `0.005` ticks) and `> 500 HKD` equity bands follow the identical band-lower-bound anchor formula `(anchor + floor_or_ceil((d_price - anchor)/step) * step)` (`hkex_rules.py:138-147`); testing multi-band transitions across `[10, 500] HKD` plus `3053.HK` (`120.00 HKD`) exercises every branch of `round_hkex_price`. |
| All 6 gap/news-shock tiers (`HOLD_FLASH_CRASH_GUARD < -12%`, `SELL_SLICED_TWAP [-12%, -5%)`, `SELL_NORMAL >= -5%`, `SKIP_ADVERSE_GAP_DOWN < -3%`, `BUY_NORMAL [-3%, +3%]`, `BUY_STAGED_50 (+3%, +6%]`, `SKIP_GAP_UP > +6%`) plus `HALTED_OR_NO_QUOTE`, `HOLD_AT_TARGET`, wide-spread passive-mid downgrade, and min-notional churn filtering (`full_exit` exemption). | Representative 3-symbol universe (`S1 = "0002.HK"` Part A stock, `S2 = "2828.HK"` Part D ETF, `DEF = "3053.HK"` Part B defensive fallback). By linearity of `redirected_defensive += redir` (`policy.py:738, 760, 785`) and pairwise transitivity of tuple sorting (`policy.py:890-891`), 2 non-defensive symbols + 1 defensive symbol suffice to witness any budget-leak, board-lot, tick-band, or sort-order inversion. |
| Exact `floor_to_lot`, 3-slice `split_qty_into_lot_slices`, SEHK leading-zero stripping (`"0002.HK" -> "2"`), and two-phase `phase_a_sells` -> `phase_b_buys` dispatch order. | Working open orders and `completed_intents` are fixed to empty (`0` / `{}`) at `L0`; `L3` models open-order offsets and persisted `completed_intents` concretely. |

## Solver choice
**TLC explicit-state BFS** — finite, loop-free per-symbol evaluation pipeline (`S1 -> S2 -> DEF -> SORT -> EXEC`); explores the full Cartesian product of initial holdings, target budgets, and quote regimes in seconds (`primary` + `scale` configs at `VIBE_TLA_EFFORT=high`).

## Concurrency derived from the runtime (not the source file)
`build_execution_batch` (`policy.py:280-906`) executes synchronously in memory on the single Python main thread *after* `get_positions()`, `get_cash()`, `get_open_orders()`, and `get_quotes()` return immutable frozen dataclasses (`PositionSnap`, `CashSnap`, `OpenOrderSnap`, `Quote`). Zero socket I/O occurs inside `build_execution_batch`, so no interleavings with `ib_async` callbacks exist within `L0`.
