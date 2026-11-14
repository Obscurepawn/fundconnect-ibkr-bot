# 03_run_log — L1_TwsWalkLimitAndRaces

| # | UTC | cmd / cfg | states (gen / distinct) | depth | wall | verdict | notes |
|---|---|---|---|---|---|---|---|
| 1 | 2026-11-14T22:42:17Z | `coverage.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_base.cfg --det` | 668 / 380 | 19 | 1s | PASS (16/16 actions fired) | Verified 0 zero-coverage actions across all bot & TWS async transitions. |
| 2 | 2026-11-14T22:42:22Z | `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_base.cfg --det` | 668 / 380 | 19 | 0s | PASS (0 errors) | `TypeOK`, `LimitNeverExceedsSlippageCeiling`, and `NoDuplicateOrderOnModify` hold across full state space (`BUY` & `SELL`, 3 walk steps, partial/full fills, `Inactive`, `ApiCancelled`, `Error 104`, `WORKING` timeout). |
| 3 | 2026-11-14T22:42:24Z | `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_modify_race.cfg --det` | 74 / 47 | 7 | 1s | VIOLATED (`NoModifyOnTerminalOrder`) | Candidate L1-A: async terminal transition (`Inactive`/`Filled`/`ApiCancelled`) during `get_quotes()` causes `modify_order_limit` (`broker.py:468`) to invoke `ib.placeOrder` on a `DoneStates` trade (`AssertionError`). |
| 4 | 2026-11-14T22:42:27Z | `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_stale_report.cfg --det` | 48 / 35 | 6 | 1s | VIOLATED (`FillAccountingExact`) | Candidate L1-B: async fill/reject during `get_quotes()` followed by `compute_walked_limit_price(...) is None` (`policy.py:1040-1041`) breaks out of walk loop and returns stale pre-`get_quotes()` `FillReport` (`policy.py:1046`). |
| 5 | 2026-11-14T22:42:46Z | `run_tlc.sh L1_TwsWalkLimitAndRaces.tla L1_TwsWalkLimitAndRaces_error104.cfg --det` | 102 / 67 | 9 | 1s | VIOLATED (`FillAccountingExact`) | Candidate L1-C: server-side `Error 104` ("Can't modify a filled order") during `modify_order_limit` (`broker.py:467-472`) leaves `trade.order.lmtPrice` mutated to the rejected walk price (`10014` vs venue `10008`) and inflates `reprice_count`. |

## Fix validation (`L1_TwsWalkLimitAndRaces_v2`)

| # | UTC | cmd / cfg | states (gen / distinct) | depth | wall | verdict | notes |
|---|---|---|---|---|---|---|---|
| 6 | 2026-11-14T23:26:04Z | `run_tlc.sh L1_TwsWalkLimitAndRaces_v2.tla L1_TwsWalkLimitAndRaces_v2.cfg --det` | 1,250 / 660 | 19 | 1s | PASS (0 errors) | All 6 invariants (`TypeOK`, `LimitNeverExceedsSlippageCeiling`, `NoDuplicateOrderOnModify`, `NoModifyOnTerminalOrder`, `FillAccountingExact`, `CoarseTickWalkReachesNextTick`) hold across `BUY`/`SELL`, `WALK_LOOP`/`DIRECT_MODIFY`, and fine (`1 bps`) + coarse (`16 bps > 6 bps`) tick regimes. |
| 7 | 2026-11-14T23:26:05Z | `run_tlc.sh L1_TwsWalkLimitAndRaces_v2.tla L1_TwsWalkLimitAndRaces_v2_scale.cfg --det` | 1,352 / 708 | 21 | 0s | PASS (0 errors) | High-effort scaling check (`MaxWalkSteps = 4`, `ReqQty = 300`). |
| 8 | 2026-11-14T23:26:08Z | `coverage.sh L1_TwsWalkLimitAndRaces_v2.tla --all-cfgs --det` | 1,250 / 660 & 1,352 / 708 (+ 5 v1 cfgs) | 19–21 | 3s | PASS (20/20 actions fired in all 7 cfgs) | Confirmed 0 zero-coverage actions across `_v2.cfg`, `_v2_scale.cfg`, and all 5 v1 counterexample configs (`_modify_race.cfg`, `_stale_report.cfg`, `_error104.cfg`, `_base.cfg`, `.cfg`). |
