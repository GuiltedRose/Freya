# Market pipeline recovery

Implementation is unexecuted. No build, test, model training, RPC, indexer, or
activation was run by the implementation agent. Run the following on the monolith.
Existing wallets, ledgers, model files and checkpoints have not been changed.

## Prerequisites that cannot be inferred

* Reviewed RPC storage rights, and separate ML/evaluation/oracle rights where used.
  Keep unverified flags false. Pump web collectors remain disabled. On-chain
  discovery supplies market leads, not social narratives or human-buyer proof.
* Authoritative Pump and PumpSwap IDLs matching the deployed versions. The recorded
  unknown eight-byte event tails remain quarantined. Do not trim bytes or add guessed
  fields. Put `pump.json`, `pump_amm.json`, and their `.manifest.json` files in a
  readable directory outside the Nix store, such as `/etc/crypto-bot/schemas`.
  Each manifest needs the original `program`, exact file `sha256`, `reviewed: true`,
  and `evidence` identifying the authoritative source, version and review.
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
* `startSlot` to the explicit reviewed finalized start slot for a new index. On a
  resume the saved checkpoint wins. Collect forward for at least a complete hour.
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

Oracle outage, old prices, incomplete coverage and unknown layouts fail closed.
Held positions remain recorded; do not manufacture exits at stale prices.
Repair authorization/config/schema problems, inspect health, and restart at the
saved checkpoint. Do not delete checkpoints or rewrite old conversions.

```bash
sudo systemctl stop crypto-bot-live.service crypto-bot-paper.service crypto-bot-indexer.service
sudo systemctl stop crypto-bot-health.timer crypto-bot-unified-paper-learning.timer
sudo systemctl stop crypto-bot-unified-paper-learning.service
sudo systemctl stop solana-budget-network.service
sudo nixos-rebuild switch --rollback
```

Omit nonexistent optional learner units. Keep the repair directory and all old state;
rollback changes software/configuration, not databases. Review the restored units
before restarting the old workflow. No automatic restore overwrites later trades,
training evidence, models, or checkpoints.
