# Coordinated collection, paper, learning and live operation

This startup integration is **implemented but unexecuted**. The operator must run
the new tests and deployment checks. Earlier component-test results do not validate
these changes or establish profitability/readiness of a real market pipeline.

## What starts

`nix run .#paper` now invokes `run_bot_pipeline.py --mode paper`, rather than only
the paper consumer. It starts a finalized RPC indexer and a bounded periodic learner,
waits for their actual database schemas, then starts the paper consumer with both
database paths and the reviewed model configuration. The data path is unchanged:
native Rust transport → Python finalized index → identity-bound markets/candles →
momentum reclaim → paper/live decisions → cost-adjusted learning.

The runner reads only an allowlist of public collection/path settings from `.env`.
It does not load signing fields into its environment. Indexer and learning workers
receive environments with signing credentials removed. Only explicitly requested
live execution passes signing settings to the existing wallet loader. Nothing is
written into the Nix store from `.env`, private config, wallets or operational state.

`SOLANA_RPC_URL` is used in a reviewed source config only when its URL is explicitly
`${SOLANA_RPC_URL}` and its hostname is included in `policy.allowed_rpc_hosts`.
Literal configured source endpoints keep their own reviewed identity. Provider
URLs/keys are not printed in pipeline status. Resolved per-session config copies
are retained under the owner-only pipeline directory; protect these files because
a provider URL may contain an API key. Wallet keys are never copied into them.

## First configuration

Run from the repository directory. Keep `flake.lock` ignored and use the local
file explicitly; none of these commands updates it:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --configure
```

The interactive wizard gathers explicit source, oracle, IDL-compatibility and ledger
provenance evidence. It uses the existing paced, read-only native RPC client to
discover mainnet finalized deployment slots and validate the oracle account/layout,
freshness and confidence. It never loads signing fields or starts services.
It saves `onchain_pipeline.json` and `learning_pipeline.json` with mode 0600 and
retains exact owner-only backups of existing files before replacement. New path settings are
optional; defaults match these filenames and `state/onchain/index.sqlite`,
`state/paper_volume`, `state/meme_live`, and `state/ml/unified.sqlite`.

Interactive first-time `--initialize` and live `--execute` launch this wizard
automatically when config files are absent, then continue startup after setup passes.
Without a terminal, missing configuration produces `configuration_required`; paper
initialization creates unapproved templates. `--prepare-config` still creates only
unapproved templates and preserves existing files. Parse failures are reported without cascading
errors about fields in a configuration that could not be loaded. A valid endpoint
and wallet key alone cannot supply the review evidence listed below.

The same wizard is available through `.#live -- --configure`; live startup is
`.#live -- --execute`, without a paper prerequisite. Setup never authorizes signing.
Forward-only setup approves a range from its observed head while requiring the
same deployed program slot on every ingestion batch; upgrades remain quarantined.
An older existing index checkpoint requires historical reviewed ranges: the wizard
refuses to skip/reset it. Isolated/self-hosted configurations retain their dedicated
network-budget setup and are not replaced with a remote-provider policy.
For rollback, stop owned services and restore the reported `*.json.backup-*` files
to their original config paths; retain all ledgers, models and checkpoints.
For example, substitute the actual backup IDs printed by setup:

```bash
cp -- onchain_pipeline.json.backup-<ID> onchain_pipeline.json
cp -- learning_pipeline.json.backup-<ID> learning_pipeline.json
chmod 600 onchain_pipeline.json learning_pipeline.json
```

The `<ID>` markers are placeholders, not literal shell input. Re-run `--preflight`
before restarting the selected runner. Keep the backups until acceptance completes.

The actual remaining operator inputs are:

* Source storage, stream if used, ML/evaluation and oracle permissions/evidence for
  the selected provider. Remote providers explicitly require `remote_provider`
  mode and its review flag; home-node transfer accounting is not required for that
  mode. Self-hosted sources retain their existing enforced network/session policy.
* Exact reviewed program deployment slot and inclusive observation range for each
  pinned IDL hash. Template numeric fields are null; observing a current deployment
  does not prove that an old IDL matches it or authorize historical observations.
* Reviewed, identity-bound USD oracle feeds and a confidence limit. Each traded
  quote mint needs a conversion; USDC is not assumed to equal one dollar. Current
  oracle snapshots do not backfill past rates. Unknown event layouts stay quarantined.
* Learning rights/provenance for selected ledger records. The learning config must
  reference both actual paper and live ledger paths; absent ledgers are reported
  as not created, without fabricating observations. Configure candidate IDs only
  after reviewing their independent evidence. No model is auto-promoted.

The runner reports missing fields together:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --preflight
```

Preflight makes no RPC connection and opens no new trading/model databases. It
does inspect existing database identities read-only, so an incorrect path cannot
silently turn a paper/model database into an index. Template evidence such as
`REPLACE` remains a blocker even if someone changes a flag to true.

## Paper startup and resume

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --initialize
```

Initialization is explicit: the existing paper CLI supplies virtual defaults
0 USDC, 0.1 SOL, illustrative $100/SOL. Override these with its inventory flags.
An existing paper database is never reset; omit `--initialize` on resume. The
coordinated path refuses `--reset` and feed replay; the raw app remains available
for deliberate independent replay/repair. `--no-loop` performs one consumer cycle
and stops the workers this invocation owns. `Ctrl+C` stops owned consumers/workers
gracefully, preserving checkpoints, positions, pending-order journals and models.

Only an empty index without historical blocks/ranges may discover its initial
finalized slot automatically. The selected start is durably saved before ingestion.
An existing cursor always wins. Historical records without a cursor require
explicit recovery, never an automatic jump forward.

Starting collection now does not produce an immediately complete previous hour.
Expect forward warmup for the full observation window and contemporaneous oracle
coverage. `throughput_insufficient`, growing slot lag or `keeping_up: false` means
the provider/ingestion path cannot sustain fresh complete observations. No pacing,
coverage or freshness gate is relaxed to force trades. A public RPC endpoint can
respond successfully while still lacking production throughput/history.

**Paper starts smaller using the existing percentage sizing.** Before sufficient
Kelly evidence, paper uses its existing rules-based entry budget multiplied by the
independent allocation tier (initially 25% of that budget, configurable through
`allocation_steps` and its bounds). There is no separate strategy or fixed-dollar
account. All reserve, liquidity, cost, drawdown and exposure vetoes still apply.
Each persisted sizing decision identifies `paper_percentage_warmup` and reports
zero measured Kelly ceiling: warmup is not an estimated edge. No allocation is
authorized for nonpositive observed expectancy, stale/revoked evidence, failed-order
costs, or sufficiently sampled negative/uncertain edge. Zero means no trade.

With no configured unified candidates, this paper warmup can gather actual outcomes
from momentum reclaim. Configured model vetoes are always respected; absent models
are never marked qualified. Once evidence supports Kelly, its ceiling and separate
allocation tier govern sizing, and the normal model-selection requirements apply.
Live also permits reduced percentage warmup without prior paper outcomes. Neither
mode reports warmup as supported Kelly evidence. Optional `--neural-mode qualified`
requires model qualification and disables live percentage warmup. Size can change
with equity, evidence and allocation tiers; concurrency progresses independently.

## Shared services and live wiring

For paper and live consumers running simultaneously, use an independently owned
data supervisor rather than tying live data lifetime to a paper terminal:

```bash
# Terminal 1: finalized collection and periodic learning, no trading.
nix run --reference-lock-file ./flake.lock --no-write-lock-file .#data
# Terminal 2: use --initialize only for a new paper run.
nix run --reference-lock-file ./flake.lock --no-write-lock-file .#paper -- --external-workers
```

Add the experimental-feature flags above if your Nix configuration needs them.
`--external-workers` verifies matching locked worker configurations, including
the learner's frozen capital policy. Consumers do not stop external workers.
Duplicate writers and incompatible source/learning configurations are rejected.
The standalone `.#collect` mode owns only the finalized indexer.

The live app uses these same data/model databases. It **requires** the explicit
`--execute` flag before loading a wallet or starting services. A key in `.env` is
not authorization. When the operator eventually authorizes live operation, use
`.#live -- --external-workers --execute`. No such command was run here. The existing
source checks, circuit breakers, capital/exposure controls and order reconciliation
remain in the live engine. Paper history and ten consecutive promotions are not
requirements for live activation. Paper-first operation is strongly recommended.

The default `--neural-mode learning` accepts empty model history; configured models
can still veto entries on their scores. Model qualification remains independent
and is never fabricated. `--neural-mode qualified` is an optional stricter policy;
NixOS exposes `services.crypto-bot-paper.live.neuralMode` (default `learning`).
The execution journal, status, buy context and capital decisions record the mode.
No extra qualification-waiver flag is required. Source permissions/freshness,
portfolio limits, TP/SL, emergency stops, signer authorization and pending-order
reconciliation still apply. A market signal, sufficient capital and acceptable
costs are still required; enabling live never guarantees an entry.

Recovery is available with `nix run .#live -- --execute --manage-only`
(and `--external-indexer` when an indexer is separately
owned). It disables all new entries and manages only existing positions and pending
orders, without requiring the ML database/configuration. Valid market/oracle data,
execution authorization and durable execution journals still apply. A malformed
optional exit model cannot suppress hard TP/SL. Normal runtime learning-refresh
failures block new entries while existing holdings continue through management.

## Monitoring, acceptance and rollback

Mode-specific `state/<consumer>/pipeline/<mode>/status.json` reports worker starts,
data warmup, incomplete coverage, throughput warnings, worker failures and shutdown.
`indexer.log`, `learning.log`, and `consumer.log` are owner-only. A failed data worker
does not get silently restarted, and the consumer is retained for existing-position
management. Credentials, order bytes and RPC API keys are excluded from status.

Operator-only checks (not executed by the implementation agent):

```bash
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_pipeline_startup.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_pipeline_setup.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_dynamic_capital.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_paper_startup.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_model_decisions.py
nix develop .#native-rpc -c python -m unittest discover -s tests -p test_production_pipeline.py
```

Include the ignored local lock/feature flags as needed. Stage the new source/test
files before Git-backed flake builds; never stage private configs, `.env` or state.
Then run the documented preflight, supervised paper collection, real-source freshness
and hour coverage checks, interruption/restart, learner failure and pending-order
recovery checks. No synthetic/loopback result proves profitability or live readiness.

To roll back orchestration, stop its owned processes, retain all state/config/model
files, and use the existing raw apps with separately supervised data workers. Do
not downgrade software while partial/scaled positions require its accounting.
NixOS deployments retain explicit manual services: learning configuration is required
for paper/live entries, while `live.manageOnly` provides the restricted recovery path.
