# 01_reasoning — L3_CrashRetryIdempotency

## Abstraction choices
- **Two-Run Crash/Interleaving/Retry Composition (`run_num \in {1, 2}`)**: Models a representative Phase A sell leg (`InitSellQty = 100` `SGOV.US` shares) funding an optional FX leg (`BootstrapFxUsd = 30`) and a Phase B buy leg (`TargetBuyQty = 100` shares). Two sequential invocations of `execute_market_rebalance` suffice to expose every persistence, intent-identity, FX-idempotency, and cross-`clientId` visibility bug.
- **Explicit Broker Order Book Tagged with `client_id`**: Each open order in `sell_orders` / `buy_orders` carries `[id, client_id, rem_qty, status]`. Modeling `IbAsyncBrokerAdapter.get_open_orders` (`src/fundconnect_bot/execution/broker.py:338`) as filtering `o.client_id = active_cid` (`ib.reqOpenOrders()`) directly contrasts client-scoped visibility against global account open-order reality (`TotalWorkingSell`, `TotalWorkingBuy`).
- **Exact `completed_intents` & Scaled-Buy Intent Semantics**: Matches `policy.py:535-537` (`intent in disk_completed` skips slice), `policy.py:1005` (`replace(o, quantity=scaled_qty)` preserves `"BUY_INTENT"`), `policy.py:1125-1126 & 1217-1218` (`status in ("FILLED", "PARTIAL", "WORKING")` persists intent), and `policy.py:1145-1148` (`convert_usd_to_hkd` neither checks nor updates `ExecutionState`).

## Code -> TLA+ mapping
| Code location | Construct | TLA+ action / operator |
|---|---|---|
| `src/fundconnect_bot/execution/broker.py:336-369` & `policy.py:316-323, 442-561` | `get_open_orders` (`reqOpenOrders`) + `build_execution_batch` | `VisibleWorkingQty`, `BuildBatch` |
| `src/fundconnect_bot/execution/policy.py:1116-1137` | Phase A walk outcomes, crash window, `save_execution_state` | `PhaseAFillFull`, `PhaseAFillPartial`, `PhaseAStayWorking`, `PhaseASkip`, `CrashBeforePhaseASave`, `SavePhaseAAndProceedToFx` |
| `src/fundconnect_bot/execution/policy.py:990-1008, 1145-1205` | `convert_usd_to_hkd` + `apply_cash_cap_to_buys` (`replace(o, quantity=...)`) | `FxAndCapBuys` |
| `src/fundconnect_bot/execution/policy.py:1208-1230` | Phase B walk outcomes, crash window, `save_execution_state` | `PhaseBFill`, `PhaseBStayWorking`, `PhaseBSkip`, `CrashBeforePhaseBSave`, `SavePhaseBAndFinishRun` |
| `src/fundconnect_bot/cli.py:378-430` & TWS venue | Inter-run fills/cancels & CLI retry invocation | `InterRunTwsFillWorking`, `InterRunTwsCancelWorking`, `StartRetryRun2` |

## Expected state space & bounds
- `InitSellQty = 100`, `TargetBuyQty = 100`, `BootstrapFxUsd = 30`, `Run2ClientIds = {26, 27}`, `FxConfigs = {"NO_FX", "WITH_FX"}`.
- Estimated distinct states: ~600–2,500 states (`< 2s` in TLC), with 100% action coverage in `L3_CrashRetryIdempotency_base.cfg`.

## Uncertainties / questions for code check
- None (`0` UNCLEAR/TODO).
