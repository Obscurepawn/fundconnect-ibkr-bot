# 00_input — L0_GapSizingAndHkex  (FROZEN at close)

## Inputs from the previous level
- Base level (`L0`): none. Establishes the single-snapshot sizing, HKEX microstructure, and deterministic two-phase batch contract exported to `L1`–`L3`.

## New code references modeled at this level
- `src/fundconnect_bot/execution/hkex_rules.py:39-65` — `HKEX_PART_A_SPREAD_BANDS` and `HKEX_ETP_PART_D_SPREAD_BANDS` spread tables.
- `src/fundconnect_bot/execution/hkex_rules.py:87-106` — `hkex_tick_size` minimum price variation lookup across Part A, Part B (`3053.HK`), and Part D.
- `src/fundconnect_bot/execution/hkex_rules.py:109-148` — `round_hkex_price` directional tick quantization (`BUY` -> `ROUND_FLOOR`, `SELL` -> `ROUND_CEILING`) anchored at band lower bounds.
- `src/fundconnect_bot/execution/hkex_rules.py:198-230` — `floor_to_lot` whole-lot share flooring and `split_qty_into_lot_slices` board-lot-preserving TWAP slicing.
- `src/fundconnect_bot/execution/hkex_rules.py:233-255` — `to_ibkr_contract_fields` SEHK leading-zero stripping (`"0002.HK" -> "2"`).
- `src/fundconnect_bot/execution/policy.py:194-230` — `_select_style_and_limit` protective floor, passive mid (wide spread / patient), and bounded marketable limit pricing.
- `src/fundconnect_bot/execution/policy.py:280-906` — `build_execution_batch` 6-tier gap/news-shock classification, `redirected_defensive` budget conservation, min-notional churn guard, and deterministic Phase A / Phase B sorting.
- `src/fundconnect_bot/execution/policy.py:1116-1122` — Phase A sells dispatched prior to Phase B buys (`src/fundconnect_bot/execution/policy.py:1208-1214`).
- `src/fundconnect_bot/config.py:213-240` — `ExecutionPolicyConfig` default gap, spread, min-notional, and slippage constants.

## Supplied context and its verification status
- Gap classification & adjusted target sizing live inside `build_execution_batch` (`src/fundconnect_bot/execution/policy.py:616-887`), `round_hkex_price` (`src/fundconnect_bot/execution/hkex_rules.py:109-148`), and `floor_to_lot` / `split_qty_into_lot_slices` (`src/fundconnect_bot/execution/hkex_rules.py:198-230`) — VERIFIED at `src/fundconnect_bot/execution/policy.py:280-906` and `src/fundconnect_bot/execution/hkex_rules.py:87-255`.
- IBKR `Error 110` tick validation and `Error 200` SEHK leading-zero contract rules — VERIFIED in `docs/ibkr_tws_api_semantics.md` (§1.3) and `src/fundconnect_bot/execution/hkex_rules.py:233-255`.
