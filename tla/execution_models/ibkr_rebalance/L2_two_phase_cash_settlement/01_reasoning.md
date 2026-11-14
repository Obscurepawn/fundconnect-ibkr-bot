# 01_reasoning — L2_TwoPhaseCashSettlement

## Abstraction choices
- **Scaled Integer Currency Units (`1 unit = $1,000 USD` or `HK$1,000`, FX rate `1 USD = 8 HKD` integer)**: Preserves exact clearing-house conservation (`settled_cash + unsettled == cash_balance`) and proportional lot scaling (`LotUnitCost = 10` units) without floating-point state explosion.
- **Explicit Multi-Day Clearing Pipeline (`day \in 0..3`)**: `pending_settle[ccy][d]` records signed cash obligations clearing on day `d \in {1, 2}`. Advancing `day: 0 -> 1 -> 2 -> 3` after `execute_market_rebalance` directly exposes settlement mismatches (such as spending `T+2` FX USD proceeds on `T+1` US equity buys, which temporarily drives `settled_cash["USD"] < 0` on `day = 1` before recovering on `day = 2`).
- **Four Canonical Initial Portfolio Scenarios**:
  1. `"SAME_SESSION_US"`: `0` settled USD, `100` `SGOV.US` Phase A sell (`T+1`) funding `90` US Phase B buys (`T+1`) -> verifies same-cycle `T+1 == T+1` netting causes zero margin loan when Phase A fills (and caps buys to `0` if Phase A stays `WORKING`).
  2. `"HK_UNSETTLED_FX_HKD"`: `0` settled HKD + `240` unsettled `T+2` FX HKD -> verifies `compute_spendable_cash` (`policy.py:961-962`) blocks premature `HK` buys when `require_settled_hkd = True`.
  3. `"UNSETTLED_FX_USD"`: `0` settled USD + `100` unsettled `T+2` FX USD (`CashBalance = 100`, Margin `AvailableFunds = 200`) -> tests whether `compute_spendable_cash` (`policy.py:961-965`) gates unsettled `T+2` USD before `T+1` US buys.
  4. `"MONDAY_BOOTSTRAP_FX"`: `0` settled USD, `100` `SGOV.US` Phase A sell (`T+1`), `bootstrap_fx_usd = 30`, `allow_fx_convert = TRUE`, Margin `AvailableFunds` bonus `150` -> tests `convert_usd_to_hkd` (`broker.py:514-526`, `policy.py:1145-1148`) routing (`IDEALPRO` vs `FXCONV`) and settled-USD gating.

## Code -> TLA+ mapping
| Code location | Construct | TLA+ action / operator |
|---|---|---|
| `src/fundconnect_bot/execution/broker.py:280-334` | `IbAsyncBrokerAdapter.get_cash` | `BrokerCashSnap` |
| `src/fundconnect_bot/execution/policy.py:1116-1141` | Phase A sell execution + post-sell cash refresh | `ExecPhaseASellFilled`, `ExecPhaseASellUnfilled`, `RefreshCashAfterSells` |
| `src/fundconnect_bot/execution/policy.py:1145-1155` & `broker.py:488-538` | `convert_usd_to_hkd` vs reserve branch | `ExecBootstrapFx`, `SkipOrReserveFx` |
| `src/fundconnect_bot/execution/policy.py:954-1009, 1177-1205` | `eff_cash`, `compute_spendable_cash`, `apply_cash_cap_to_buys` | `ComputeSpendableAndCapBuys` |
| `src/fundconnect_bot/execution/policy.py:1208-1214` | Phase B capped buy execution | `ExecPhaseBBuys` |
| `docs/ibkr_tws_api_semantics.md:53-93` | `T+1` US / `T+2` HK & FX clearing | `AdvanceSettlementDay`, `SettlementDone` |

## Expected state space & bounds
- 4 scenarios x 2 Phase A outcomes = ~60–120 distinct states (`< 1s` in TLC), with 100% action coverage in `L2_TwoPhaseCashSettlement_base.cfg`.

## Uncertainties / questions for code check
- None (`0` UNCLEAR/TODO).
