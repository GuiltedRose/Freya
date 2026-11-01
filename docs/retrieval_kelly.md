# Retrieval expectancy, capital ceilings and independent tiers

Momentum reclaim remains the only production entry strategy. Discovery/candles
continue through the native RPC pipeline. This implementation is unexecuted: the
operator must run all tests, builds, source validation and activation.

## Evidence and retrieval

Historical neighbors now report net expectancy in USD, mean win/loss payoff,
downside RMS, an uncertainty interval for the win frequency, and a conservative
bootstrap lower bound for expectancy. A low win frequency does not veto a positive
expectancy neighbor set. The RAG decision uses supported net expectancy, not a
2/3 accuracy threshold. Other models retain independent qualification.
Positive cost-normalized expectancy must also be supported; unknown cost basis
cannot pass the retrieval gate. Versioned ledger imports append corrected grouping
records and revoke superseded imports from qualification without deleting history.

Retrieval excludes future/unknown labels, stale observations, revoked records,
duplicate outcomes, duplicate positions and repeated correlation groups. Candidate
validation purges entire chronological groups across train/validation/test folds.
Training corpus IDs and risk parameters are frozen in new candidate fingerprints;
old models/checkpoints are retained. Validation and test reports include paired
reclaim-without-retrieval comparisons, net P&L, drawdown, group-bootstrap uncertainty,
Brier score and calibration buckets. Calibration statistics are assessments, not
guarantees that a model confidence is a true win probability.

Prospective RAG promotion requires independently recorded pre-entry predictions,
confirmed cost-adjusted live outcomes, enough independent groups and observed days,
positive lower-bound incremental expectancy, no worse drawdown, a passing held-out
comparison and coverage of failed-order costs. Existing model lineage/expiry checks
and the independent ten-consecutive-promotion live gate remain mandatory.
Rejected candidates require observable baseline outcomes for evaluation: missing
counterfactual fills are never invented. Insufficient prospective evidence stays
collecting; synthetic replay cannot qualify a live model.

## Kelly and performance tiers

`capital_policy.example.json` exposes provisional conservative parameters; review
them rather than treating the defaults as finalized strategy constants. Both runners
load it by default, and accept `--capital-config /absolute/operator/config.json`.
Nix exposes `services.crypto-bot-paper.capitalConfigFile` for the same file.

Kelly uses realized net return divided by verified trade cost, independent of model
confidence. It deduplicates correlated groups, requires both win and loss evidence,
uses a lower probability bound and conservative payoff bounds, and limits allocation
with fractional Kelly, an absolute fraction cap and a ruin buffer. Failed-order fees
are conservatively apportioned across the independent cost basis before estimating
edge. Missing/negative/uncertain edge means **zero allocation**, never a risk increase.
Legacy outcomes without reconstructable cost basis are excluded.

The calculated ceiling is further reduced by the unlocked allocation tier, remaining
portfolio exposure, existing position/bankroll/stop-risk limits, reserves, liquidity
and execution overhead. The model may choose any fraction from zero through one of
the remaining permitted amount (`allocation_fraction` in the reviewed model config).
Paper recomputes the fill at that amount; live rebuilds/simulates the actual input.
Model confidence never increases the ceiling. Existing TP/SL and circuit breakers
remain in force. Automatic virtual bankroll refills are disabled under this policy.

Allocation and concurrency maintain separate tier indices, streaks, thresholds,
step arrays and minimum/maximum indices. Defaults start allocation utilization at
25% of the conservative Kelly ceiling and capacity at one. Their default promotion
thresholds differ (five versus ten wins), and are independently configurable. One
promotion never calls or promotes the other system. Their conditions can independently
be satisfied by the same outcome; they do not share counters. Maximum utilization
is 100% of the calculated ceiling, and maximum capacity is 20.

`capital_policy`, `capital_outcomes`, `capital_revocations` and `capital_decisions`
are additive tables in each existing paper/live SQLite journal. Close processing
updates accounting and tiers in the same transaction; outcome IDs prevent repeat
promotion after restart. Decisions persist evidence IDs, bounds, tier state, capital,
exposure, model fraction and reasons. Corrections revoke evidence without deleting
it and conservatively relock tiers. Change parameters by explicitly increasing the
configuration `version`; previous versions remain stored, and the new version starts
with conservative tiers. Parameter changes within an existing version are blocked.
Rolling back to an older version blocks new entries; use a new reviewed version
with the desired parameters. Existing positions remain manageable.

## Shared live ownership and recovery

Live state keeps a mint-keyed position book alongside the compatible `position`
alias. Existing positions are retained when portfolio accounting is enabled. Only
one position per mint is permitted. Every pending sell identifies its owning position;
recovery verifies that identity before accepting a receipt. Closing one position
cannot clear another. A single wallet-wide pending order serializes submission:
pending/uncertain buys reserve capital and block replacement, including after a crash.
There is no automatic replay of signed bytes.

The loop services each held position independently before discovery. A quote outage
for one mint does not skip the others. Aggregate bot drawdown includes marked P&L
across the book. Stale marks or incomplete bankroll valuation block new allocations.
Demotion does not liquidate existing positions; it prevents excess new entries.
ML/configuration failures block entries without gating existing position exits.
Loss of connectivity cannot produce a valid new fill or receipt: retain positions
and uncertain orders, report the outage, and resume reconciliation when connectivity
returns. An uncertain wallet-wide order intentionally blocks further submission;
this preserves ambiguity and shared-wallet safety rather than pretending exits filled.
Twenty is a hard upper bound, not a claim that current network budgets can sustain
twenty fresh markets. Freshness and all existing network budgets remain enforced.

## Operator validation

```bash
nix develop "path:$PWD#native-rpc" --command python -m unittest discover -s tests -p test_dynamic_capital.py
nix develop "path:$PWD#native-rpc" --command python validate_production.py --native --nix --output /tmp/crypto-bot-capital-validation.json
```

The deterministic suite covers asymmetric low-win-rate positive expectancy, negative
and uncertain edge, stale/correlated/duplicate evidence, independent promotion and
demotion, zero/intermediate model allocation, shared exposure, capacity 20, policy
versioning, restart idempotency, ownership and uncertain/confirmed order recovery.
The aggregate suite also includes the existing pipeline, TP/SL, reserve, circuit
breaker and journal tests. Run the privileged kernel pass separately as documented
in `production_acceptance.md`; no such command was run by the implementation agent.

To persist a separate sizing comparison, supply JSON `{"outcomes": [...]}` with
each row containing `id`, `group_id`, `entry_at`, `exit_at`, `pnl`, `cost_usd`,
`equity_usd`, `fixed_cost_usd`, `eligible: true`, and
`net_of_costs_verified: true`. Supply actual recorded cost/equity evidence, not
fabricated labels. The comparison only uses earlier closed groups at each decision:

```bash
python run_unified_learning.py --db /absolute/ml/unified.sqlite \
  --capital-config /absolute/capital.json \
  --sizing-evaluation-input /absolute/reviewed-comparison.json
```

Results are stored immutably in `evaluations`. The resizing comparison adjusts fixed
costs but remains a modeled counterfactual; its output cannot qualify live trading.
Live performance improvement requires prospective evidence under the unchanged
reclaim baseline. Real schemas, source permissions, genuine cost/qualification data,
monolith builds, tests and operator acceptance remain activation prerequisites.

For rollback, stop the live service first and follow `pipeline_recovery.md`. Preserve
all journals, models, versions, outcomes and revocations. Never restore an older
database over newer confirmed trades or erase an unresolved order to restart.
