# One production strategy: momentum reclaim

Sniping and its new-pool discovery runner have been removed. Normal market discovery
remains available through the finalized index. Production paper/live commands reject
non-reclaim rules. Old signal implementations remain readable for historical ledgers
and regression tests; they are not selectable production strategies.

The one strategy uses closed candles to identify impulse → pullback → reclaim.
Its buy-volume, concentration, reverse-route simulation, cost, reserve, drawdown,
position, cooldown and live-order authorization controls remain enforced.
Paper defaults to one position, a one-hour loss cooldown and active neural vetoes;
it no longer takes random below-threshold entries for exploration.

## Components and actual decisions

| Component | Input | Decision role |
| --- | --- | --- |
| Reclaim policy | Finalized USD candles and directional volume | Only production entry signal |
| Existing neural entry model | Eligible outcome features | Qualified production entry veto |
| SVM | Independently annotated risk outcomes | Safe-market entry veto |
| LSTM | Eight chronological feature observations and completed-position outcomes | Entry outcome veto |
| Historical retrieval | Eligible past outcomes, excluding correlated/future labels | Context-based entry veto |
| Existing neural exit model | Recorded position quotes and later net quotes | Qualified early exit; ordinary stops, targets and time exits remain active |

Unified models are enabled by configuring their database and reviewed candidate IDs.
They actively veto entries rather than merely logging predictions. All three families
are required when unified models are enabled. Scores cannot authorize transactions,
increase sizing or override safety vetoes. Prospective qualification is an optional
stricter live policy (`--neural-mode qualified`); the default learning mode permits
live cold starts without paper history. Paper-first operation is recommended.

Missing state and credentials are expected on this development host. No replacement
wallet, balance, model, checkpoint or `.env` has been created. Configure actual runtime
paths on the monolith using [pipeline recovery](pipeline_recovery.md).

## Run everything together on the monolith

Nothing below was executed by the implementation agent. From the repository:

```bash
nix develop "path:$PWD#native-rpc" --command python validate_production.py \
  --native --nix --output /tmp/crypto-bot-validation.json
```

Choose a new output filename for each run; reports are never overwritten. The runner
discovers every Python test module, collects failures/errors/skips, enables the opted-in
native loopback stress suite, and then invokes the flake checks. It does not start bot,
indexer, training or trading services. Python test sockets are restricted to loopback;
provider accounting is temporary, signer environment variables are removed, and access
to repository operational state/credentials is rejected. Nix may fetch build inputs.
Privileged kernel checks are excluded unless explicitly selected. For the complete
host/kernel pass on the monolith, with no production workloads running:

```bash
sudo nix develop "path:$PWD#native-rpc" --command python validate_production.py \
  --native --kernel --nix --output /tmp/crypto-bot-validation-kernel.json
```

This explicitly enables existing isolated network-namespace tests. Never run those
privileged tests beside production collection. An ordinary run records omitted kernel
tests and cannot report complete host validation. Skips never count as full coverage.
The report always retains `live_ready: false`: synthetic tests cannot certify real
source data, live receipts or profitability.

`test_production_pipeline.py` covers synthetic RPC block ingestion, strict event
decoding, causal USD conversion, candle caching, reclaim entry, duplicate suppression,
restart, paper exit and learning. Additional tests check active model vetoes, missing
qualification, exits during model failures, authorization, transaction ambiguity,
network budgets, retention and recovery. Real-schema acceptance remains a separate
operator requirement, including authentic accepted receipts for both venues.

## Manual live integration after acceptance

The Nix module now packages `crypto-bot-live` and provides a disabled-by-default
`crypto-bot-live.service`. Enabling this service is explicit live-order authorization:
it invokes `--execute`, but remains manual-start and bound to the same finite network
session. No test or training job starts it.

Set `services.crypto-bot-paper.live.enable = true` only after operator review. Set
`live.environmentFile` to a protected runtime file containing `SOLANA_RPC_URL`, and
`live.keypairFile` to the existing external signer file. Systemd loads the signer as a
private credential; do not copy it into the repository or Nix store. Live and paper
have separate state directories. Optional saved-wallet collection needs an explicit
watchlist; a missing historical watchlist no longer prevents normal discovery.

For every contributing RPC/oracle source, record `execution_verified: true` and
`execution_evidence` only after actual source/receipt validation and operator review.
Use a new versioned source ID when changing immutable policy. Current complete
coverage and fresh quotes are checked in addition to this review. Unknown schema
layouts and missing conversions remain blocked. Paper history and ten-promotion
milestones do not gate live activation; never fabricate model qualification evidence.

If unified decisions are enabled, include the actual `bot_live` ledger path in the
learning configuration so confirmed receipts feed prospective evaluation. The learner
continues importing eligible paper/live outcomes and quote sequences for retraining.
SVM needs independent risk labels through the reviewed JSONL importer; losses are not
automatically risk labels. A fresh installation with no qualified models must report
the missing evidence explicitly, not call itself validated. Percentage-sized learning
trades still require genuine strategy signals and execution evidence.

After operator build, test and source checks, the operator may start live directly:

```bash
sudo systemctl start crypto-bot-health.service
sudo systemctl start crypto-bot-live.service
journalctl -u crypto-bot-live -f
```

Start the reviewed network session and indexer first as described in pipeline recovery.
An unqualified source/model/strategy returns a visible blocked status. Pending orders
are reconciled by their original signature. Stop the live service before rollback;
retain its order journal, wallet files, checkpoint and model evidence.
