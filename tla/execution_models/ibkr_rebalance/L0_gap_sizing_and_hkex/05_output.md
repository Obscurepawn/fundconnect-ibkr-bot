# 05_output — L0_GapSizingAndHkex  (FROZEN at close)

## What this level proved
- **`BudgetConservation`**: Across all 6 gap/news-shock sizing tiers, halted quotes, and defensive fallback evaluations (`src/fundconnect_bot/execution/policy.py:616-887`), `redirected_def` equals the exact sum of unspent incremental buy budgets (`SKIP_ADVERSE_GAP_DOWN`, `SKIP_GAP_UP`, and the unspent 50% of `BUY_STAGED_50`), `eff_notional[SymDef] == base_notional[SymDef] + redirected_def`, and total incremental buy notional never exceeds total planned incremental notional (`PASS`, 112,383 distinct states).
- **`NoNegativeOrNaNQty`**: Invalid/halted quotes (`HALTED_OR_NO_QUOTE`, `src/fundconnect_bot/execution/policy.py:626-646`) emit zero orders and `delta_qty == 0`; every emitted `OrderSpec` has strictly positive integer `quantity` and `limit_cents` (`PASS`, 112,383 distinct states).
- **`HkexTickAndBoardLotValid`**: `round_hkex_price` (`src/fundconnect_bot/execution/hkex_rules.py:87-148`) produces exact HKEX Spread Table ticks across Part A (`0002.HK`, including the `200.05 HKD` band boundary `100..200 -> 0.10` vs `200..500 -> 0.20`), Part B (`3053.HK -> 0.05`), and Part D (`2828.HK -> 0.02` in `(20, 100]`), rounds `BUY` down (`limit_cents <= raw_limit_cents`) and `SELL` up (`limit_cents >= raw_limit_cents`), strips SEHK leading zeros (`"0002.HK" -> "2"`), and enforces exact board-lot multiples (`PASS`, 112,383 distinct states).
- **`SellBeforeBuyDeterministicOrder`**: `build_execution_batch` (`src/fundconnect_bot/execution/policy.py:890-906`) places all `SELL` slices into `PHASE_A_SELL` sorted by `(symbol, slice_index)`, places all `BUY` slices into `PHASE_B_BUY` sorted by `(symbol == defensive_sym, symbol, slice_index)` (routing `3053.HK` strictly last), and dispatches every Phase A sell before any Phase B buy (`PASS`, 112,383 distinct states).

## Finding
none (`safe-by-design` across batch sizing, HKEX Spread Table rounding, board-lot slicing, and two-phase ordering).

## Fidelity & coverage  (see references/coverage.md)
- Fidelity: 13 Next-disjunct transitions + `Init` modeled, all cited `file:line` (`src/fundconnect_bot/execution/policy.py:194-906`, `src/fundconnect_bot/execution/hkex_rules.py:87-255`, `src/fundconnect_bot/config.py:122-240`); 0 UNCLEAR/TODO; fixed-point cents (`100 = 1.00 HKD`) and representative 3-symbol universe documented in `01_reasoning.md`.
- Code coverage: modeled 9 of 9 pure sizing/microstructure functions & tier branches in `hkex_rules.py` and `policy.py:build_execution_batch` (`round_hkex_price`, `floor_to_lot`, `split_qty_into_lot_slices`, `to_ibkr_contract_fields`, `_select_style_and_limit`, all 6 gap tiers + halted + wide-spread + min-notional + defensive redirect + Phase A/B sort; broker/TWS I/O deferred to L1–L3).
- Model coverage: TLC `--coverage` (`coverage.sh --all-cfgs --det`) — all 14 action(s) fired in `L0_GapSizingAndHkex.cfg` (14,047 distinct states) and all 14 action(s) fired in `L0_GapSizingAndHkex_scale.cfg` (112,383 distinct states); 0 zero-coverage actions.

## Exports to the next level
- `HkexTickAndBoardLotValid`: every `OrderSpec` entering `_execute_orders_with_walk` (L1) carries `quantity > 0`, board-lot-aligned shares, and a valid tick-rounded initial `limit_price`.
- `SellBeforeBuyDeterministicOrder` & `BudgetConservation`: `execute_market_rebalance` (L2, L3) receives deterministic non-empty/empty `phase_a_sells` followed by `phase_b_buys` whose un-capped buy notionals conserve the weekly plan budget.
