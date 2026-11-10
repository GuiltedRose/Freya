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
