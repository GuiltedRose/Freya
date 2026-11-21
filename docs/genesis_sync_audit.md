# Genesis synchronization audit and correction

## Policy and data flow

`sync_policy.json` is the authoritative fresh historical boundary (slot 0).
`sync_policy.py` reads it for the indexer, both observer entry points, ingestion
and native planning. Nix copies the same JSON into the Agave peer build, where
`follow.rs` embeds it. The Python worker source filter and install phase include
it as well. There is no tip-derived default.

Fresh boundary or explicit fresh override → persisted **requested** cursor →
mainnet/source validation → finalized head and history availability → bounded
produced-slot discovery → block/shred retrieval → parent/hash continuity and
receipt/decoder validation → transactional persistence → committed cursor →
retry unresolved history → eventually follow the current finalized head.

An existing valid checkpoint wins over an explicit start argument. Existing data
without a checkpoint, malformed state, discontinuous committed ranges, or legacy
unanchored skip tails require explicit recovery. Nothing is reset to hide an
error. A nonzero `--start-slot` is an intentional fresh-only override; it cannot
certify coverage before that boundary. Nix `startSlot = null` selects this policy.

Slot 0 is a boundary, not a required synthetic/retrievable block. RPC ingestion
begins block discovery at slot 1 and commits the boundary only together with a
retrieved successor whose parent proves continuity. Native acquisition records
an explicit Genesis-boundary proof. These are different progress scopes.

## Root causes and changes

| Component | Original defect or audited boundary | Result |
|---|---|---|
| `run_onchain_indexer.py`, `solana_observer.py` | Empty indexes selected finalized head | Shared, transactional Genesis initialization without tip lookup |
| `run_local_observer.py`, direct `Ingester.step()` | Missing explicit start could fail instead of selecting the fresh policy | Shared initialization; orphan data still blocks |
| `sync_policy.py`, `onchain_store.py` | Requested cursors and committed history were indistinguishable; old skip-only tails could look successful | Separate `committed_next_slot`, request state, resume integrity checks; retained legacy state is preserved and rejected when unresolved |
| `onchain_ingest.py` | Trailing/empty `getBlocks` responses could certify skips without an anchor; optional history probing could be disabled | Mandatory pre/post history-floor probes; no trailing skip-only commits; persisted bounded lookahead until a successor is retrievable |
| `onchain_store.py` | First retrieved parent was not checked against the requested boundary | Parent/previous-blockhash continuity is enforced, including first-block gaps; invalid commits roll back |
| Ingestion and native planner history floors | Comparing requested slot numerically with earliest produced slot also rejected genuinely skipped early slots | A higher floor is usable only when the retrieved earliest block's parent proves the requested gap; a missing produced parent still blocks |
| `prepare_native_window.py` | Native plans treated manifest omissions as skips | Explicit parent proofs, bounded successor search and separate `history-gap.json`; fresh floor recheck before atomic plan publication |
| Native `follow.rs` | Skip advancement trusted an omission list; native cursor could be mistaken for executed/committed ledger progress | Parent proofs required, old skip files audited, acquisition cursor explicitly scoped; `ledger_committed_next_slot` remains null |
| Native gossip/repair | Contact freshness and current shred version do not establish historical retention | Existing public-peer, signature, nonce, version and same-peer checks retained; historical availability explicitly unknown; timeouts never become skips |
| `rpc_contract.py` | Snapshot-skip, long-term-storage and missing slot-history/leader-context errors had inconsistent history classification | `-32007/-32009/-32019/-32021/-32022` remain unavailable history, eligible only for explicitly configured failover; never skips |
| Historical persistence/decoding | Valid null `blockTime` could prevent all early history from persisting; current program versions quarantined unrelated pre-program blocks | Nullable time with transactional row/index/trigger-preserving migration; unknown times stay null; no fabricated events; deployment qualification required for relevant transactions |
| `observer_archive.py`, `observer_retention.py` | Time-dependent archive logic assumed integer timestamps | Null-time archival carries no invented conversion window; automatic retention excludes unknown-time evidence |
| Streaming and continuation | Recent notifications must not select historical progress | Hints remain separate from finalized coverage; unresolved discovery waits/paces instead of spinning at positive lag |
| `prepare_native_runtime.py`, `prepare_mainnet_host.py` | Private config/ledger excluded from Git disappeared after a fresh checkout | Explicit offline bootstrap from a portable copy of the existing reviewed config; existing files never replaced; missing history is recorded, not fabricated |
| `flake.nix`, `nix/native-peer.nix`, `nix/module.nix` | Shared policy must survive filtered source packaging | JSON included in Python/native packages; explicit start override documented; service/network boundaries unchanged |
| `nix/freya-operators.nix` | Host bootstrap and immutable deployment can import separate copies of the operator module | Stable module key deduplicates declarations without changing privileges or membership policy |

## Gap, persistence and recovery semantics

`getBlocks` omissions alone do not prove a skip. Freya additionally requires a
retrieved finalized successor with a parent below the omitted slot. Stored parent
slot and previous blockhash must match the durable predecessor. Missing/pruned
blocks, snapshot gaps, malformed evidence and failed repair attempts do not advance
the committed prefix. An empty bounded response moves only discovery lookahead;
the synchronized cursor remains unchanged. Lookahead survives restart, is pinned
to the configured source/cursor and never enlarges a single request's slot budget.

`history_requests` distinguishes requested, unresolved, blocked and committed
retrieval state. `next_slot` is the next requested position; before any successful
commit it is only an initialized boundary. `committed_next_slot` is backed by an
anchored persisted range. Native `acquisition_next_slot` only represents acquired
shreds or explicit reviewed parent proofs; it is not executed ledger ingestion.
RPC receipts remain authoritative at `native_handoff.reconcile`.

Null-time blocks retain their actual null timestamp and quarantine time-based
use. Their transactions are never turned into invented timestamped training
observations. Current deployment/IDL ranges and causal historical USD-oracle
requirements are not extended back to Genesis. Unknown relevant layouts remain
quarantined. Raw full-block retention remains explicit (`--retain-blocks` for the
indexer; observer retention remains enabled); hashes alone do not certify replay.

The nullable-time writer migration preserves existing rows and schema-owned
indexes/triggers in one transaction, rejecting unfamiliar block columns. Before
upgrading an existing large index, stop its writer and take an owner-only SQLite
backup with SQLite's `.backup` command; allow space for a second block-metadata
table. Do not copy a live SQLite database without its WAL. Readers remain read-only.

## Audit coverage and deliberately retained boundaries

Reviewed live entry points, `Ingester.step/follow`, Python/Rust RPC contracts and
read-method allowlists, stream recovery/acknowledgements, native planning/cache,
resume/checkpoint publication, peer selection, repair send/receive/reconstruction,
receipt reconciliation, store coverage, archive/retention, pipeline setup and
supervision, Nix source filtering/packaging and operator runbooks.

`pipeline_setup.py` samples current deployment slots to qualify current decoder
versions, not to choose an ingestion boundary; those ranges must not be relabeled
as historical review. Wallet signature-history lookups, live reconciliation,
diagnostic transport probes, fixture timestamps and offline replay bounds are
not historical synchronization defaults and were not blindly replaced with zero.
The Rust transport remains read-only; Python owns decode/storage/checkpoints.

Lease duration, byte caps, provider pacing, disk/shred limits, namespace isolation,
finite retry caps, `Restart=no`, wallet/live authorization and model qualification
remain in force. A finite exhausted repair budget stops with an unresolved gap;
it does not authorize unlimited retries or permission/budget evasion. Bootstrap
is an explicit new accounting epoch after acknowledged loss, not restoration of
unknown cooldowns/denials. Known provider restrictions must still be honored.

## Actual local validation

| Check | Actual result |
|---|---|
| Genesis/bootstrap/planning/startup/indexer/block-batch/RPC-contract group | 102 passed |
| Observer/stream/retention/archive/receipt/pipeline/hour group | 82 passed |
| Ingestion audit/pipeline startup/production paper integration/shared cooldown | 36 passed |
| Current native peer production sources in cached pinned Agave workspace | Compiled; 44 native tests passed |
| Loopback HTTP → recorded block → Python/Rust transport → decode → SQLite | 1 integration test passed, covering 48 fixture/backend/encoding/chunking combinations |
| Modified Nix expressions | Parsed successfully |
| Operator module imported from two distinct paths | Evaluated to one option declaration |
| Production Python worker package | Built offline; packaged `freya-indexer --help` succeeded with shared JSON policy present |
| Native-node/native-peer production derivations | Offline dry-run evaluation succeeded; full Nix builds not executed |

The native build used identical current `peer.rs`, `state_lock.rs`,
`acquisition.rs` and `follow.rs`, plus the shared policy copied into the cached
workspace. Reproduce the Nix peer build on Monolith rather than relying on that
temporary path. An initial local native link failed when the 16-GiB `/tmp` filled;
generated incremental caches and the failed link artifact were removed, then
compilation and tests passed. Interrupted fixture runs were rerun successfully.
Sandbox-only async wakeup timeouts disappeared outside the restrictive sandbox.
No external RPC, Monolith writes, activation or historical session occurred during
this audit. No live trading was run.

Reproduce all 220 focused Python tests inside the native-mainnet development shell:

```bash
python - <<'PY'
import unittest
names = '''genesis_sync native_runtime_bootstrap native_window_planning
observer_start_slot onchain_indexer block_batch rpc_backend_contract
solana_observer onchain_stream_candles rpc_stream_failover observer_retention
observer_zstd observer_migration_stress native_receipt_handoff pipeline_repair
runtime_completion selfhost_observer onchain_hour ingestion_audit
pipeline_startup production_pipeline rpc_shared_cooldown'''.split()
suite = unittest.TestSuite()
for name in names:
    suite.addTests(unittest.defaultTestLoader.discover('tests', pattern=f'test_{name}.py'))
raise SystemExit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
PY
```

Individual focused checks, loopback integration and production native builds:

```bash
python -m unittest discover -s tests -p test_genesis_sync.py
python -m unittest discover -s tests -p test_native_runtime_bootstrap.py
python -m unittest discover -s tests -p test_native_window_planning.py
python -m unittest discover -s tests -p test_observer_start_slot.py
python -m unittest discover -s tests -p test_onchain_indexer.py
python -m unittest discover -s tests -p test_rpc_backend_contract.py
python -m unittest discover -s tests -p test_production_pipeline.py
SOLANA_NATIVE_LOOPBACK_STRESS=1 python -m unittest discover -s tests -p test_native_transport_stress.py -k test_recorded_event_bytes
nix build --no-write-lock-file .#native-peer .#native-node
```

The final command builds the production binaries and runs the peer regressions;
it does not activate services. Full fresh-install prerequisites, reviewed config
bootstrap, activation, monitoring and state-preserving rollback commands are in
[the fresh-start runbook](native_fresh_start.md). Include **all new files** in the
operator's transfer/commit; Git-backed flakes omit untracked policy/config files.

## Remaining limits and outstanding acceptance

Follow-up on Monolith (`fdbd471`): the native-peer and native-node Nix packages
built successfully; native Nix tests reported 44 passed. The host's base rebuild
had removed the Freya units (`LoadState=not-found`), so reinstallation needs fresh
host preparation rather than `--refresh`. Bootstrap exposed retired decoder
hashes in the copied source config and correctly published no runtime files.
The setup correction validates native metadata-source transport separately and
grants no decoder qualification; full ingestion still rejects those stale pins.
Fresh provider-ledger creation is explicitly operator-authorized, but had not run
successfully at this point. This does not establish operational Genesis readiness.

Subsequent Monolith revision `8a6cb73`: explicit fresh bootstrap succeeded. The
Freya add-on built and passed dry-activation, then was activated with NixOS `test`
(temporary, not a boot-profile switch). The three units were restored with
`Restart=no`, and nftables was active. One 55-second/1-MB bounded session ran with
planner run ID `000000000000000018e699d5f8779e2e`. Genesis validation, cluster
discovery, finalized-head and history-floor stages passed; `getSlotLeaders`
failed with `RpcEvidenceError`. No cursor advanced, no repair requests were sent,
and zero shreds were validated. The controller expired cleanly without retries.
The budget charged 1,000,000 bytes; observed IP traffic increased by 404,054 bytes
download and 11,656 bytes upload. The native workload remained failed and all
Freya networking stopped. Prior network accounting and identity were retained.

Terminal evidence/request exceptions did not retain provider codes, so these
diagnostics cannot establish whether this was a provider rejection or malformed
response. A local diagnostic correction preserves bounded `rpc_code`/`source_id`
without raw provider messages, changing neither exception classes nor retries;
31 RPC-contract/native-planning regressions passed. That correction needs an
operator transfer before another separately authorized diagnostic session.

Live Genesis synchronization is **not established** by these tests. The configured
public source has not been proven archival, nor have discovered peers been proven
to retain Genesis shreds. Gossip contact information does not provide a historical
retention guarantee; current shred-version compatibility cannot certify decoding
every historical shred format. The native engine remains a selective acquisition
and reconstruction bridge, not a validator executing the entire historical chain.

Historical metadata/leader schedules, archival RPC block availability, compatible
historical shred formats, and reviewed old program schemas are external/protocol
requirements. Failure to obtain any required evidence remains an explicit gap;
there is no automatic tip jump, fabricated block, widened IDL range or fake receipt.
The bootstrap source does not supply an archive or newly qualify historical data.
Remaining acceptance is the operator's full Nix build and a separately authorized,
budgeted run against sources that actually supply the requested historical range.

Primary protocol references: [getFirstAvailableBlock](https://solana.com/docs/rpc/http/getfirstavailableblock),
[getBlocks](https://solana.com/docs/rpc/http/getblocks),
[getBlock and nullable blockTime](https://solana.com/docs/rpc/http/getblock),
[Agave RPC errors](https://github.com/anza-xyz/agave/blob/master/rpc-client-api/src/custom_error.rs).
The successor-parent requirement is Freya's conservative continuity policy; an
omission or RPC error is not independently treated as proof of a skipped slot.

## Monolith follow-up

The run on `378fb35` launched the planner and reached `getSlotLeaders`, which
returned `-32602` for the fresh Genesis window. It validated zero shreds and
left the checkpoint unadvanced. The request uses two positional integers,
`[0, 4]`, within Agave's accepted count. Agave also returns this code when
the requested epoch's schedule is unavailable, independently of finalized block
availability. The numeric code alone cannot establish which provider condition
occurred. [Agave RPC implementation](https://docs.rs/crate/solana-rpc/4.4.1/source/src/rpc.rs#11013)

The planner now records `rpc_reason: leader_schedule_unavailable` for the
specific Agave response. Check that the error and supervisor records share a
run ID. If that reason is present, require a reviewed source with the Genesis
leader schedule before another acquisition attempt. If the reason is absent,
ask the provider whether its endpoint serves `getSlotLeaders([0, 4])`.
Before enabling acquisition on any replacement source, verify Genesis leader
coverage, finalized history continuity, and compatible retained shreds.
Receipt and decoder qualification still apply before ledger ingestion.

Monolith reproduced the failure on `978ce43` with matching supervisor and
planner run IDs and the explicit `leader_schedule_unavailable` reason.

Yellowstone Old Faithful is a candidate for independently checking finalized
block history. Its published epoch-zero recap covers slots 0 through 431999 and
identifies the CAR by CID and SHA-256. Its documented RPC interface supports
historical blocks and slot lists. Native acquisition also requires a source of
Genesis leader identities and original signed shreds. Those are outside the
documented Old Faithful RPC interface. Keep the current diagnostic disabled
until the needed evidence is available.
[Epoch-zero recap](https://files.old-faithful.net/0/0.recap.yaml)
[Old Faithful RPC methods](https://github.com/rpcpool/yellowstone-faithful#usage)

### Finalized RPC backfill source review

The published epoch-zero CID matches the recap. The publisher's validation
report records a successful PoH check and a terminal block at slot 431999.
The published slot list begins with consecutive slots 0 through 11, including
the entire first Freya window. This review retrieved only archive metadata, a
bounded prefix of the slot list and the validation log.
[Published PoH report](https://files.old-faithful.net/0/poh-check-0.log)
[Published slot list](https://files.old-faithful.net/0/0.slots.txt)

Freya's finalized indexer can backfill without historical leader schedules.
It still requires finalized loader accounts to verify active decoder ranges.
The standalone Old Faithful RPC does not advertise `getAccountInfo`, and its
archive lags the live chain. A production configuration needs an explicitly
reviewed source for loader accounts and a fresh finalized feed, with provider
accounting and provenance preserved across the archive-to-live transition.
Validate the boundary's parent slot and blockhash using the existing continuity
checks before the live feed takes over. An archive manifest or PoH report does
not qualify missing execution receipts; Old Faithful documents missing
transaction metadata in some early epochs.
[Old Faithful archive](https://docs.old-faithful.net/)
[Early-epoch metadata limits](https://docs.old-faithful.net/usage/validation/sharp-edges)

### Running the finalized backfill

Use a separate index and a private runtime configuration with exactly two
enabled, individually reviewed RPC endpoints. Add the following to the indexer
configuration, using the actual configured source IDs. The example boundary is
the last produced slot in the published epoch-zero archive. Use it only if the
live endpoint can serve the successor and the remaining finalized history.

```json
{
  "archive_backfill": {
    "archive_source_id": "old-faithful-reviewed",
    "live_source_id": "mainnet-reviewed",
    "through_slot": 431999
  }
}
```

The archive serves the finalized manifest and blocks through the configured
boundary. The live endpoint supplies the finalized head and loader accounts
throughout, and the configured oracle must also use it. Once the archive
boundary is committed, the indexer reads subsequent blocks from the live
endpoint. The first produced live block must pass the existing parent-slot and
blockhash checks against the archived prefix. Choose a produced block for the
boundary so the archive can anchor it. Missing history blocks progress rather
than falling back across the boundary. Retained full blocks record which
source supplied the loader-account evidence.

Before activation, check both endpoints' source rights, approved network
budgets, deployment pins and actual archive-to-live coverage. Use a new
private state directory for this index. The NixOS indexer service reads the
configuration via its existing systemd credential path.

```bash
nix develop --offline --no-write-lock-file .#native-rpc --command \
  python -m unittest discover -s tests -p test_onchain_indexer.py
nix build .#native-rpc-app --no-write-lock-file
./result/bin/freya-health --config /etc/crypto-bot/pipeline.json
```

Use the [pipeline recovery runbook](pipeline_recovery.md) for activation and
rollback. Watch the indexer journal for `block_source_id`, `state_source_id`,
`archive_through_slot`, the saved cursor and coverage. Paper startup still
requires fresh complete market data. To roll back, stop the new indexer and
restore the previous service configuration while retaining both index
directories and the provider ledger.

### Local Genesis smoke test

On `zelda`, the loopback Old Faithful service returned the mainnet Genesis
hash and finalized slots 1–3. Both adjacent parent hashes matched and slot 1
reported parent slot 0. All three blocks omitted `blockTime`. The archive
indexer treats an omitted timestamp as unknown and keeps the block in the
quarantine path for time-based use.

The reviewed live RPC reported finalized ProgramData deployment slots
469811025 for Pump and 469811067 for PumpSwap. Neither matches the saved
configuration. Both bundled schema hashes also differ from the saved pins.
No pins or production checkpoints were changed.

A separate laptop smoke test used the real archive and live loader-account
reads with no approved deployment ranges in the disposable index. It committed
slot 1 from Genesis, reopened at checkpoint 2, then committed slot 2 and
advanced to checkpoint 3. Both blocks retained their parent links and
quarantine issues for missing timestamps, transaction metadata and unreviewed
versions. The test wrote zero market events. This verifies archival persistence
and resume on the laptop. Production decoder qualification and the live handoff
remain separate acceptance gates.

The laptop's reviewed live RPC reported a history floor of 0. Its finalized
block 432000 referenced the parent slot and blockhash of Old Faithful's block
431999. This verifies the boundary pair without replaying the intervening epoch.

On November 21, Rust/Python comparison accepted one finalized swap per venue
with no decoder issues, at slots 472040458 (Pump) and 472040504 (PumpSwap).
These samples establish compatibility for those receipts. Deployment-wide
schema ranges and sustained paper readiness remain unqualified.

### NixOS archive service

Import Freya's default NixOS module and add these settings alongside the
reviewed indexer runtime configuration:

```nix
services.crypto-bot-paper = {
  enable = true;
  rpcMode = "hybrid";
  stateDirectory = "/var/lib/freya-archive-backfill";
  providerLedger = "/var/lib/freya-provider/provider_limits.sqlite";
  indexerConfigFile = "/etc/crypto-bot/pipeline.json";
  archive = {
    enable = true;
    configFile = "/etc/crypto-bot/old-faithful-epoch.json";
  };
};
```

Set the archive RPC endpoint to `http://127.0.0.1:18889`, with
`network_mode: isolated` and `allow_loopback_http: true`. Keep the reviewed
live endpoint and its provider accounting. Stage the archive data and indexes
in a service-readable location outside home directories. The published epoch-zero
CAR alone is 4,286,945,461 bytes. The existing 1 MB session cannot stage it,
and fetching the epoch-zero slot list on every start would also exceed that
allowance. Use staged local files in the Old Faithful epoch configuration.
Set `providerLedger` to the retained shared database. If the original ledger
is under a home directory, stop its writers before relocating it and any
SQLite sidecars to a private service-readable directory. Update every
consumer to the same location before restarting. The indexer requires an
existing writable ledger at the configured path.
[Archive configuration format](https://docs.old-faithful.net/running-old-faithful/installation-and-setup/configuration-files/local-file-system)

The archive and indexer share the existing network namespace and lease. The
archive is bound to namespace loopback. Start the indexer only after checking
the committed state and reviewed source policies. It starts the archive and
waits for its listener before ingestion.

```bash
sudo systemctl start crypto-bot-indexer.service
sudo journalctl -u crypto-bot-old-faithful -u crypto-bot-indexer -n 60 --no-pager
sudo systemctl start crypto-bot-health.service
```

Enable and start paper after the health command succeeds and the operator's
reviewed learning configuration and virtual inventory are in place. Stop
`crypto-bot-indexer.service` and `crypto-bot-old-faithful.service` before
rolling back the host configuration. Preserve the archive files, provider
ledger and all index state. See [pipeline recovery](pipeline_recovery.md).
