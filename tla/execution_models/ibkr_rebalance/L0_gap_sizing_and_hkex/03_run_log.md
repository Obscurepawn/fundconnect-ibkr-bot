# 03_run_log — L0_GapSizingAndHkex

| # | UTC | cmd / cfg | states (gen / distinct) | depth | wall | verdict | notes |
|---|---|---|---|---|---|---|---|
| 1 | 2026-11-14T22:34:30Z | `run_tlc.sh L0_GapSizingAndHkex.tla L0_GapSizingAndHkex.cfg --det` | — / — | — | 1s | parse error (exit 150) | SANY rejected reserved TLAPS keyword `DEF` on line 69 and record-set comprehension syntax in `Init`; renamed `DEF` -> `SymDef` and rewrote `base_notional` function-set comprehension. |
| 2 | 2026-11-14T22:37:50Z | `run_tlc.sh L0_GapSizingAndHkex.tla L0_GapSizingAndHkex.cfg --det --coverage` | 16,991 / 14,047 | 12 | 3s | PASS (0 errors, 14/14 actions fired) | Verified `TypeOK`, `BudgetConservation`, `NoNegativeOrNaNQty`, `HkexTickAndBoardLotValid`, `SellBeforeBuyDeterministicOrder` across all 8 quote/gap regimes (`HALTED`, `FLASH_CRASH`, `SEVERE_DOWN`, `ADVERSE_DOWN`, `MILD` @ `200.05` boundary, `WIDE_MILD`, `MOD_UP`, `SURGE_UP`). |
| 3 | 2026-11-14T22:38:15Z | `coverage.sh L0_GapSizingAndHkex.tla --all-cfgs --det` | 16,991 / 14,047 & 130,959 / 112,383 | 12 | 6s | PASS (14/14 actions fired in both cfgs) | Confirmed 0 zero-coverage actions across both `L0_GapSizingAndHkex.cfg` and `L0_GapSizingAndHkex_scale.cfg`. |
| 4 | 2026-11-14T22:38:34Z | `run_tlc.sh L0_GapSizingAndHkex.tla L0_GapSizingAndHkex_scale.cfg --det` | 130,959 / 112,383 | 12 | 7s | PASS (0 errors) | High-effort scaling check (`LotS1 = 500`, `InitQtySet = {0, 500, 1500}`, `BaseBudgetSet = {0, 1500000, 10000000}`) exhausting partial trims, hold-at-target, min-notional filter, 3-slice TWAP, and defensive redirects. |

## Sync validation (`L0_GapSizingAndHkex_v2`)

| # | UTC | cmd / cfg | states (gen / distinct) | depth | wall | verdict | notes |
|---|---|---|---|---|---|---|---|
| 5 | 2026-11-14T23:20:49Z | `run_tlc.sh L0_GapSizingAndHkex_v2.tla L0_GapSizingAndHkex_v2.cfg --det` | 16,991 / 14,047 | 12 | 2s | PASS (0 errors) | Re-anchored `ExecuteBatchStep` (`policy.py:1132-1314`) and `BatchDone` (`policy.py:1331-1340`) after Phase-2 edits below line 1004; all `build_execution_batch` & `hkex_rules` lines (`194-906`, `87-250`) unchanged. |
| 6 | 2026-11-14T23:20:57Z | `run_tlc.sh L0_GapSizingAndHkex_v2.tla L0_GapSizingAndHkex_v2_scale.cfg --det` | 130,959 / 112,383 | 12 | 8s | PASS (0 errors) | High-effort scaling re-verification on `L0_GapSizingAndHkex_v2`. |
| 7 | 2026-11-14T23:21:13Z | `coverage.sh L0_GapSizingAndHkex_v2.tla --all-cfgs --det` | 16,991 / 14,047 & 130,959 / 112,383 | 12 | 15s | PASS (14/14 actions fired in all 4 cfgs) | Confirmed 0 zero-coverage actions on `L0_GapSizingAndHkex_v2`. |
