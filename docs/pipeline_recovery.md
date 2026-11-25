# Market pipeline recovery

Use this guide for operator-controlled recovery, preserving wallets, ledgers,
models and checkpoints. Current checks and their limits are recorded in
[ingestion acceptance](ingestion_repair_20261102.md).

## Prerequisites that cannot be inferred

* Reviewed RPC storage rights, and separate ML/evaluation/oracle rights where used.
  Keep unverified flags false. Pump web collectors remain disabled. On-chain
  discovery supplies market leads, not social narratives or human-buyer proof.
* Authoritative schemas matching the deployed versions. The bundled SDK schemas
  include the previously missing quote-transfer-fee field; use the coordinated
  runner's `--configure` to recheck existing deployment pins. Never trim unknown
  bytes or silently promote old quarantine. A custom schema directory must contain
  `pump.json`, `pump_amm.json` and their manifests, readable by the service user.
  Each custom manifest requires the original `program`, exact `sha256`,
  `reviewed: true` and source/version/review `evidence`.
* `reviewed_program_versions` maps each program address to nonoverlapping records:
  `from_slot`, `through_slot`, `deployment_slot`, and the matching `idl_hash`.
  Bounds are inclusive; the observed deployment must match. An upgrade closes the
  gate until reviewed. Source-policy changes need a new source ID.
* Reviewed Pyth feed/mint/USD denomination and oracle confidence limit. SOL/USD is
  included as a disabled template; configure an independently reviewed USD feed for
  every other quote mint, including USDC. There is no assumed dollar peg.
* A permitted RPC with sufficient full-block history and throughput inside the
  existing network budget. Rust cannot compensate for insufficient bandwidth.
  No limiter or budget has been enlarged. A complete real hour is an acceptance
  requirement, not an implementation claim.

Copy `onchain_pipeline.example.json` and, optionally,
`learning_pipeline.example.json` to new operator-owned runtime files. Fill in
reviewed values. Templates deliberately fail preflight. Do not overwrite existing
configurations. Protect credentials with mode 0600. Schema files must be readable
by the service user; never place secrets in Nix expressions.

Oracle capture runs through the selected existing paced RPC client, every configured
10–60 seconds between index batches. It captures current evidence only. Missing
historical conversions remain missing; do not backfill them with today's price.
Oracle telemetry distinguishes failures from successful capture. A slow batch may
delay capture: health and price-age gates then block use.

## Build and offline validation

These commands include untracked source files without changing Git branches:

```bash
cd /home/zelda/repos/crypto-bot
export REPAIR_BUILD_DIR="$(mktemp -d /tmp/crypto-bot-review.XXXXXX)"
nix build .#native-rpc-app --out-link "$REPAIR_BUILD_DIR/app"
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_pipeline_repair.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_production_pipeline.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_model_decisions.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_onchain_indexer.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_onchain_hour.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_onchain_stream_candles.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_solana_observer.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_paper_runtime.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_meme_learning.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_unified_learning.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_rpc_backend_contract.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_data_compliance.py
"$REPAIR_BUILD_DIR/app/bin/crypto-bot-health" --config /etc/crypto-bot/pipeline.json
```

The final command is offline and checks configuration and native-extension presence;
it does not prove provider permissions, actual deployment compatibility, or runtime
coverage. Run authentic accepted-receipt differential tests for both venues against
the reviewed bundle before activation. Fixture parity that only rejects transactions
is insufficient. Keep unknown-layout fixtures as rejection regressions.

## NixOS integration

Point the monolith's crypto-bot flake input at this reviewed source and import its
`nixosModules.default`. In the host configuration set:

* `services.crypto-bot-paper.enable = true` and `indexerConfigFile =
  "/etc/crypto-bot/pipeline.json"`.
* A **new** `stateDirectory`, e.g. `/var/lib/crypto-bot-paper-repair`, to preserve
  existing incompatible paper inventories and quarantined index history. No reset
  or automatic state migration is needed. Existing compatible state may be resumed
  only with exactly matching initial inventory and rules.
* For the bot, leave `startSlot` unset and use `syncFrom = "recent"` to start a
  new index at the reviewed RPC's finalized head. The head must have a timestamp
  within the last 30 seconds. Use a separate index directory
  if a historical checkpoint already exists. The headless runner exposes the
  same choice as `--sync-from recent`. When changing index directories, point
  `providerLedger` or `CRYPTO_BOT_PROVIDER_LIMITS` at the existing shared
  provider ledger. Paper and live still require fresh,
  complete coverage and their existing admission checks.
* Set `syncFrom = "genesis"` or pass `--sync-from genesis` for historical
  backfill. Existing indexes resume their saved checkpoints in either mode.
  Native node and archive startup retain their historical defaults.
  See [Genesis sync audit](genesis_sync_audit.md) for source requirements.
* `paper.enable = true`, `paper.initialBalanceUsd`, `paper.initialNativeSol`, and
  `paper.initialSolPriceUsd` to your chosen virtual inventory/valuation. These are
  explicit simulation inputs, never real wallet funding. No values are inferred.
* `paper.mints` to at most five reviewed market mints if desired. On-chain discovery
  also works; unwatched markets calculate verified candles directly from events.
* `services.solana-budget-isolation.uplinkInterface` and the existing reviewed
  budget policy. Do not increase limits to make this change pass.
* Optionally `learning.enable = true` and `learning.configFile =
  "/etc/crypto-bot/learning.json"`. Set ledger paths to the selected state directory.
  The offline learner and paper process use the same `ml/unified.sqlite`.

Services remain manual-start and bound to the existing finite network session.
The alternate observer module still defaults to one validation cycle; explicitly
set `services.crypto-bot-observer.maxCycles = null` for continuous collection within
that same finite session. Do not run two writers against one index.

Build the host before activation. Set `NIXOS_FLAKE` to your actual host reference:

```bash
export NIXOS_FLAKE=/etc/nixos#monolith
sudo nixos-rebuild build --flake "$NIXOS_FLAKE"
```

Stop existing writers before taking a private backup of their entire state
directories, including WAL files and models. Do not copy an actively written
SQLite database as a backup. Keep the old NixOS generation. The new isolated state
directory avoids modifying the old experiment during acceptance.

## Activation and acceptance

Only after reviewing build/test output and source evidence:

```bash
sudo nixos-rebuild switch --flake "$NIXOS_FLAKE"
sudo systemctl start solana-budget-network.service
sudo systemctl start crypto-bot-indexer.service
sudo systemctl start crypto-bot-health.timer
journalctl -u crypto-bot-indexer -u crypto-bot-health -n 50 --no-pager
```

Wait for `crypto-bot-health` to succeed: current index, complete hour, fresh SOL/USD,
recent verified swaps, and usable selected markets. Inspect `recent_issues`, oracle
telemetry, per-market errors and lag; block counts alone are insufficient. A session
budget may expire before an hour is collected. Resume its checkpoint only through
the existing operator-authorized session workflow; automatic renewal is absent.

```bash
sudo systemctl start crypto-bot-health.service
```

After the health command succeeds, start the enabled services:

```bash
# If learning was enabled, initialize its separate database before paper starts:
sudo systemctl start crypto-bot-unified-paper-learning.service
sudo systemctl start crypto-bot-unified-paper-learning.timer
sudo systemctl start crypto-bot-paper.service
journalctl -u crypto-bot-paper -u crypto-bot-unified-paper-learning -f
```

Omit the two learner commands when learning is disabled. Initial missing paper
ledgers are reported and skipped; the next scheduled learning pass imports them.
Training is bounded to 1024 ledger outcomes and 1024 quote imports per pass; larger
histories require reviewed paged JSONL imports. Corrected/archived unified labels
are revoked without deleting evidence. Missing timestamp/net-cost/risk evidence
does not become a fabricated label. SVM remains blocked without independent risk
annotations; wallet USD outcomes require verified historical conversions.

Candidate IDs appear in learner JSON output. Pin reviewed IDs under `candidates`
(`svm`, `lstm`, `rag`) in the runtime learning config and restart the trader.
Configured models actively veto entries: every family needs an eligible score
at or above the existing 2/3 threshold. LSTM needs eight chronological observations.
Missing models, insufficient sequences, expired models and revoked evidence block
entries. Live model use additionally requires the existing prospective qualification
criteria, including confirmed outcomes and failed-order costs. Paper outcomes never
qualify a live model. Entry-model failures do not disable existing position exits.
See [production acceptance](production_acceptance.md) for aggregate tests and live wiring.

Acceptance requires fresh held quotes, a recorded paper entry and exit with costs,
visible source outages with retained positions, restart continuity, and honest
training/qualification status. No test result demonstrates profitability.

## Recovery and rollback

For a lightweight recent index, set `"selective_recent": true` in a separate
indexer configuration and start with `--sync-from recent`. Configure the
reviewed private RPC as an isolated Rust loopback source. The RPC must serve
current finalized slots, accounts-only blocks, full transaction receipts, and
loader and oracle accounts. Freya matches each selected receipt to its block
slot, signature, version, status and ordered account list. An incomplete or
conflicting receipt leaves the cursor available for retry. The mode indexes
only the configured decoder programs and cannot be used with full-block
retention or historical archive replay. Qualify the source and measure the
request rate before enabling paper.

For recent sync with redundancy, list the reviewed private RPC endpoints in
primary-to-backup order in the indexer configuration. Each must have a distinct
source ID, serve finalized block receipts and loader accounts, and pass its
existing network boundary checks. Add no public endpoint to a private-only
configuration. All endpoints use the retained provider ledger.

When a finalized block is missing, malformed, or conflicts with the committed
parent, the indexer can try the next reviewed endpoint from the saved cursor.
Previously reported produced slots remain required across restarts. A backup
that omits one cannot turn it into a skip. Check the journal for
`finalized_block_rejected`, `next_source_id`, and the checkpoint. Permission
denials, rate limits and decoder quarantine continue to use their existing
handling. Archived and native-reconciliation evidence requirements still
apply when those modes are configured.

### Decoder upgrade and feed checks

Run qualification from the deployed checkout against the retained provider
ledger. The command reads finalized loader accounts and verifies an accepted
swap per venue using both decoders. It rereads the deployments before issuing
a candidate. A supplied public transaction signature can be used when the
bounded recent sample has no accepted swap.

```bash
export CRYPTO_BOT_PROVIDER_LIMITS=/path/to/existing/provider_limits.sqlite
umask 077
freya-qualify-pipeline deployments --config "$INDEXER_CONFIG" > deployment-candidate.json
freya-qualify-pipeline feed --config "$INDEXER_CONFIG" --rounds 12 --batch-slots 32 --interval 5
```

For a specific receipt add `--receipt pump=SIGNATURE` or
`--receipt pump_amm=SIGNATURE`. Review the resulting receipts, schema
digests, finalized deployment slots and proposed forward-only boundary.
Create a separate staged config after approval:

```bash
freya-qualify-pipeline stage --config "$INDEXER_CONFIG" \
  --candidate deployment-candidate.json --review "$REVIEW_REFERENCE" \
  --output-config /etc/crypto-bot/pipeline.reviewed.json
```

The command refuses to replace an existing file or stage a candidate generated
from a different config. Preserve the old configuration and point the
indexer credential at the staged config after validation. Use a new recent
index if the saved cursor precedes the newly reviewed range and the prior
range has no compatible schema evidence. Existing indexes and provider
accounting are retained. A later loader deployment mismatch keeps affected
events quarantined. For an already authorized live instance that needs exit
or pending-order management during recovery, use the existing manage-only
workflow.

The feed command uses a disposable index and the same paced transport.
It requires the cursor to keep pace with the finalized head over the sample.
Run the ordinary index health check against the deployed index before enabling
paper. Feed sampling alone cannot establish a complete fresh hour, valid
oracle data or paper readiness.

Oracle outage, old prices, incomplete coverage and unknown layouts fail closed.
Held positions remain recorded; do not manufacture exits at stale prices.
Repair authorization/config/schema problems, inspect health, and restart at the
saved checkpoint. Do not delete checkpoints or rewrite old conversions.

```bash
sudo systemctl stop crypto-bot-health.timer crypto-bot-unified-paper-learning.timer
sudo systemctl stop crypto-bot-live.service crypto-bot-paper.service
sudo systemctl stop crypto-bot-unified-paper-learning.service crypto-bot-challengers.service
sudo systemctl stop crypto-bot-health.service crypto-bot-indexer.service solana-budget-network.service
sudo nixos-rebuild switch --rollback
```

Omit optional units absent from the host. Stop any alternate observer and its
learner before rollback if you started them against this state. If an order is
pending, retain its journal and reconcile the original signature before resuming
entries. Keep the repair directory and all old state;
rollback changes software/configuration, not databases. Review the restored units
before restarting the old workflow. No automatic restore overwrites later trades,
training evidence, models, or checkpoints.
