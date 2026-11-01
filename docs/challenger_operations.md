# Independent paper experiments and node operation

All changes are unexecuted. No profitability, measured RAM usage, successful node
bootstrap or production readiness is established. Run the checks yourself before
activation. Momentum reclaim remains the only production entry strategy; these
management experiments have no live execution adapter or live authorization.

## Implementation and evidence boundaries

* Exit liquidity/impact caps reduce paper and native live sizing beneath existing
  Kelly, bankroll, reserve and exposure caps. Missing depth cannot increase size.
  Native buy/sell output calculations constrain impact; paper costs remain modeled.
* Lifecycle context uses closed identity-bound candles. Creation age stays unknown
  without an actual creation timestamp. Regimes never become scam labels.
* `features.svm_risk_v2`, `features.lstm_continuation`, and
  `features.lifecycle_context` independently select new model inputs in the reviewed
  learning pipeline configuration. All default off. Existing artifacts are retained;
  new settings freeze new candidate fingerprints. SVM risk inputs need identity,
  freshness, provenance, schema and at least 80% measured coverage. Risk labels need
  independent incident/security/transaction analysis; price losses never suffice.
  The native collector does not currently provide all 12 advanced risk measurements
  or independently reviewed incident labels: this model stays blocked without them.
* Continuation training uses a subsequent recorded net quote 60–180 seconds later,
  excluding the completed outcome as its target. Group-purged held-out reports compare
  Brier loss against an MLP, a training-only candle-momentum predictor and a
  training-prior predictor. A lower held-out predictive
  loss does not prove executable value or probability calibration. Its new task is
  explicitly blocked from live qualification through completed-trade labels; a
  prospective quote-horizon qualification additionally requires confirmed live
  quote references within 30 seconds after the frozen prediction, 60–180-second
  future quotes, independent groups/days and predictive improvement; the existing
  net-outcome qualification must also pass. Completed winners cannot substitute
  for continuation labels. Independent chronological validation windows fit the
  continuation calibrator; probability buckets include uncertainty intervals.
  SVM scores mean risk probability: high risk vetoes entry. Its live qualification
  additionally requires independent prospective risk labels and lower Brier loss
  than the constant prior; completed winners are not substitute risk labels.
  Import reviewed annotations through the existing unified-learning JSONL command,
  binding `model_position_id` to the frozen live forecast's position ID, the same
  mint, and pre-prediction features. Supply `risk_label_evidence.reviewed/evidence`,
  a supported incident kind and basis (`onchain_transaction_analysis`,
  `independent_security_review` or `verified_incident_analysis`). Labels must become
  known after the prediction. New annotation identities append evidence; conflicting
  records cannot overwrite earlier labels. Paper/offline forecasts cannot satisfy
  the independent prospective live classification checks.
* Adaptive targets, partial sales, trailing exits, holding times, profitable scaling,
  reclaim re-entry and portfolio reductions are separate paper flags. Defaults are
  provisional, explicit and bounded in `ChallengerConfig`. Fractions at partial
  milestones apply to the remaining holding. Every slice pays execution costs;
  entry cost basis is apportioned, realized P&L is accumulated once, and loss/tier
  outcomes are recorded only when the whole position closes. Scaling requires a new
  reclaim bar, profitable position, fresh marks, liquidity and remaining Kelly and
  portfolio headroom; it never relaxes the existing stop. Existing exits run before
  entry/ML availability checks.
* Research concurrency is separate from Kelly/concurrency performance tiers. Up to
  10,000 scenarios share one compressed history journal. Only 1–8 workers run at a
  time, one scenario each; batches are 1–64 observations, snapshots at most 64 KiB,
  checkpoints at most 4 MB. Each scenario has its own bankroll, Kelly state, tiers,
  position ownership and cursor. Hashes bind evidence, leases recover worker crashes,
  and event/checkpoint writes commit atomically. RSS checks and a systemd cgroup
  bound workers; no measured memory capacity is claimed. History is never truncated.
* Paired comparisons use common-horizon, complete marked-to-market daily net changes,
  including open holdings, against the same unchanged reclaim baseline with identical
  capital and risk configuration. Missing/stale marks exclude comparison intervals.
  Simulations cannot authorize live models. Nominate one candidate per family after
  exploratory replay; only subsequent forward paper observations can qualify it.
  Promotion requires at least 30 independent daily groups, 14 days, positive lower
  bootstrap net improvement and no worse drawdown. Results and transitions persist.
  Per-family champions remain independent. Historical searches are not confirmation.

## Operator prerequisites and commands

Review `git diff` and `git status --short`; stage only the intended new source,
configuration, documentation and test files so Git-backed flakes include them.
Never stage credentials or operational state. Generate/review/retain `flake.lock`.
Use a separate new challenger database and unchanged source paper journal. Do not
reset paper balances to generate labels. Install the flake's Python dependencies,
including `zstandard`; the native RPC shell additionally supplies the Rust module.

Run these commands yourself from the repository:

```bash
nix flake lock
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_dynamic_capital.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_challenger_management.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_unified_learning.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_model_decisions.py
nix flake check --no-build
nix build .#native-rpc-app
```

Review `challengers.example.json`: set the virtual inventory, observation start and
training cutoff to your intended cohort. `rules_file: "bundled"` uses the unchanged
production rule file. Alternatively provide the exact reviewed `rules` object.
Keep scenario IDs/configuration immutable; append a new ID for a new experiment.
No eligible positive/negative Kelly evidence means zero new allocation. Seed only
reviewed, cost-adjusted, independent pre-start outcomes using `--seed-outcomes`;
never manufacture outcomes to get experiments trading. Seed sets are immutable.

```bash
# First initialization only; an existing path is deliberately refused.
nix develop .#native-rpc -c python run_challengers.py \
  --db /absolute/new/lab.sqlite --initialize --config /absolute/reviewed/scenarios.json
# Bounded read-only source import and offline work; repeat to advance the backlog.
nix develop .#native-rpc -c python run_challengers.py \
  --db /absolute/new/lab.sqlite --config /absolute/reviewed/scenarios.json \
  --paper-db /absolute/existing/paper.sqlite --run --workers 1 --jobs 128 --event-batch 16
# Freeze one selected policy for subsequent forward paper confirmation.
nix develop .#native-rpc -c python run_challengers.py \
  --db /absolute/new/lab.sqlite --nominate target-v1
nix develop .#native-rpc -c python run_challengers.py \
  --db /absolute/new/lab.sqlite --evaluate target-v1
```

Inspect `worker_metrics`, `ingest_errors`, scenarios' `status/error/cursor` and the
evaluation JSON. Correct the cause of a paused scenario, then explicitly pass
`--resume-scenario ID`. Crashed leases expire after 120 seconds; atomic cursors
prevent duplicate committed trades. Do not delete a lease, database or checkpoint.
Bounded import reads at most 64 source events per invocation; repeat batches as
needed. Increasing jobs does not implicitly increase the import budget.

Use the returned *qualified* evaluation ID explicitly:

```bash
nix develop .#native-rpc -c python run_challengers.py \
  --db /absolute/new/lab.sqlite --evaluate target-v1 --family target \
  --evaluation-id REVIEWED_EVALUATION_ID --transition promote
```

Promotion changes only the paper registry. To use it, configure
`services.crypto-bot-paper.challengers.enable/configFile` and
`paper.championFamilies = [ "target" ];`, or supply
`--paper-champion-db PATH --paper-champion-family target` to the paper runner.
Each observation rechecks selected champions. A changed/degraded/unavailable policy
blocks new allocations while existing positions retain their persisted exit policy.
A direct, unqualified **paper trial** instead uses `paper.managementConfigFile` or
`--paper-management-config`, never both modes. Its JSON contains ChallengerConfig
fields only. No new live management behavior is enabled through these settings.

The Nix module provides manual `crypto-bot-challengers.service`: private network,
read-only source ledger, writable isolated lab directory, bounded worker/jobs/RSS,
no automatic retries or timers. After host build/review, the operator may activate:

```bash
sudo nixos-rebuild build --flake /etc/nixos#YOUR_HOST
sudo nixos-rebuild switch --flake /etc/nixos#YOUR_HOST
sudo systemctl start crypto-bot-challengers.service
journalctl -u crypto-bot-challengers.service --no-pager
```

Rollback an individual paper family with `--transition rollback` and the candidate
ID/evaluation of a previously qualified champion. For baseline management, remove
the selected families and direct experiment config only after positions are flat.
For code rollback, stop experiment workers, block entries, retain current exit
management until partial/scaled holdings are flat, then stop consumers and use
`sudo nixos-rebuild switch --rollback`. Preserve all journals/models/checkpoints;
older software must not reinterpret active experimental positions. Node retention,
hardware, private RPC, bandwidth and operator startup are documented separately in
[selfhost_rpc.md](selfhost_rpc.md). The current server is not the RPC-node target.

## Remaining acceptance work

No tests, builds, simulations or node processes were executed. The operator must
validate native compilation/Agave packaging, Nix evaluation/unit behavior, journal
crash recovery, actual pool execution costs, partial-fill/order reconciliation,
and real workload RSS/disk/transfer usage. The host meter still cannot guarantee
an ISP wire-level cap; retain the upstream billing cap requirement. Advanced risk
features require complete permitted evidence; continuation needs its independent
prospective qualification before live use. None of these blockers permits weaker
source rights, fabricated labels, bypassed promotion or unbounded networking.
