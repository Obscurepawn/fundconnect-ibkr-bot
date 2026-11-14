# ROADMAP — `ibkr_rebalance`

| Level | Live Module | Mode | Status | Verdict / Finding |
|---|---|---|---|---|
| `L0_gap_sizing_and_hkex` | `L0_GapSizingAndHkex_v2` | transcription | Closed (`_v2` synced) | `PASS` (5/5 invariants verified across 112,383 distinct states; 14/14 actions covered) |
| `L1_tws_walk_limit_and_races` | `L1_TwsWalkLimitAndRaces_v2` | fault-injection | Closed (`_v2` validated) | `retired(fixed, validated by L1_TwsWalkLimitAndRaces_v2)` (6/6 invariants PASS across 708 distinct states; 20/20 actions covered) |
| `L2_two_phase_cash_settlement` | `L2_TwoPhaseCashSettlement_v2` | transcription | Closed (`_v2` validated) | `retired(fixed, validated by L2_TwoPhaseCashSettlement_v2)` (4/4 invariants PASS across 81 distinct states; 12/12 actions covered; `L2-B1` refuted per R6) |
| `L3_crash_retry_idempotency` | `L3_CrashRetryIdempotency_v2` | fault-injection | Closed (`_v2` validated) | `retired(fixed, validated by L3_CrashRetryIdempotency_v2)` (5/5 invariants PASS across 3,678 distinct states; 18/18 actions covered) |
