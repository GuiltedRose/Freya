# Selective native node

Freya keeps native ledger evidence separate from authoritative execution receipts.
The native process discovers gossip peers and requests data shreds through pinned
Agave repair protocols. A reviewed RPC provider supplies mainnet identity, finalized
slot manifests, leader schedules, shred-version claims and execution receipts.
Reconstruction does not establish transaction success, independent replay or
consensus finality. Native observations remain quarantined until reconciliation.

## Implemented path

`freya-node --peer discover|acquire|follow CONFIG STATE_DIRECTORY` selects Rust
networking without a Python network wrapper. `discover` records signed gossip
contact/epoch-slot claims. `acquire` receives data/coding shreds from explicitly
approved UDP senders. `follow` discovers compatible repair endpoints and sends
signed `HighestWindowIndex`/`WindowIndex` requests. It authenticates repair pings,
returns pongs and binds replies to socket, nonce, slot/index, leader and version.
One request is in flight, with paced retries and a durable per-window retry cap.
Timeouts never establish skipped slots or authorize checkpoint jumps.
The planner checks the RPC history floor before and after each manifest; pruned
history and retention races block instead of turning missing data into skip claims.

Raw data/parity files are fsynced before reconstruction. Agave Merkle/Reed–Solomon
recovery can recover missing data from sufficient **available** coding shreds;
recovered data is signature-checked again. Reconstruction requires all data indices
from zero through one terminal shred, respects component/entry boundaries and
checks parent consistency. Incomplete, malformed and conflicting evidence fails
closed. Conflicts and raw files are preserved. Restart reconstructs from disk.

Standard index repair requests retrieve data shreds, not coding shreds. Automatic
coding-shred retrieval is consequently **not supplied by this request path**.
Parity is usable when received through the approved passive path or already
persisted in the slot directory. No unsupported coding request is invented.
Unstaked peers may ignore, throttle or prune repair requests. Actual mainnet access
and sustained acquisition capacity remain unverified.

## Execution receipts without full-history replay

Shreds contain ordered transaction messages, not success/failure, CPI logs,
pre/post balances, loaded addresses or execution fees. Account snapshots do not
reconstruct past per-transaction receipts. Matching signatures against RPC is not
independent message execution or consensus verification.

Agave can start from a reviewed recent bank snapshot and replay forward instead
of replaying from genesis. This still requires the complete relevant bank/account,
program and runtime state and subsequent ledger dependencies; selectively replaying
only Pump transactions is insufficient. Its working set alongside Freya on 64 GiB
has not been demonstrated. The current lightweight choice retains finalized RPC
receipts and avoids local execution-state requirements.

Pinned source evidence:
[entry.rs](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/entry/src/entry.rs),
[transaction_status_service.rs](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/rpc/src/transaction_status_service.rs),
[blockstore_processor.rs](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/ledger/src/blockstore_processor.rs).

## Indexer handoff

Each complete slot publishes `SLOT/handoff.json` (`freya-native-slot-v1`), including
ordered transactions/signatures, parent, leader, verification and completeness.
It explicitly lacks execution metadata and independent finality. The native
process owns this directory; the indexer owns its separate SQLite database.

Optional `"native_handoff": "/var/lib/freya-node/follow"` in the reviewed indexer
configuration enables strict reconciliation. All ordered signatures and the parent
must match the existing finalized full-block RPC response. Required receipt fields
must be present; the existing decoder, source, deployment, oracle, candle and
market checks still run. Provenance commits atomically in `native_reconciliations`
with the existing index transaction. No extra receipt RPC request is added.

Start a **separate index database** at the same initial slot as native acquisition;
use `initial_slot` once in follow config and `--start-slot` for the indexer. If the
native checkpoint already exists, it controls resumption. Do not edit it to catch
up. Absent native evidence stops strict reconciliation at that prefix. Leave the
optional key absent for ordinary external/automatic operation. Native source
qualification remains false; discovery or reconstruction never switches trading
sources automatically. No static program filter skips receipts: loaded addresses
and indirect invocation can hide relevant activity.

## Configuration and authorization

Keep the existing reviewed collector UID, private external config, provider ledger
and network-isolation module. The external config must have one enabled Rust RPC
endpoint with reviewed storage policy. Shared HTTP pacing, 429 cooldowns and
provider identity remain enforced; the planner never reads wallet keys.

Example follow config outside the Nix store, owned privately by the collector:

```json
{
  "bootstrap_peers": [],
  "duration_seconds": 40,
  "continuous": false,
  "max_namespace_bytes": 1000000,
  "max_disk_bytes": 67108864,
  "max_shreds_per_slot": 1024,
  "max_attempts_per_window": 128,
  "request_interval_ms": 500
}
```

For follow mode, omit `bootstrap_peers` or use `[]` to obtain up to four public
IPv4 gossip addresses from the reviewed RPC's `getClusterNodes` response. Candidates
must have valid identities, matching shred versions and distinct identities/sockets.
Private, reserved and multicast addresses are rejected. Metadata is cached for at
most five minutes under the same provider identity. Old plans without bootstrap
metadata refresh without advancing their saved window; no suitable peers means
blocked. Status records `reviewed_rpc_claim` provenance. Gossip authentication
remains separate. No fetch happens during Nix evaluation, build or shell entry.

Explicit 1–4 socket lists remain supported; literal placeholders are rejected.
Discovery-only mode still requires explicit sockets. Follow plans four finalized
slots automatically; its optional
`initial_slot` is a starting anchor, not a manually enumerated schedule. Plans
retain RPC provenance and skipped-slot claims; they are distinct from native
reconstruction evidence. Version changes, lost identity, corrupt checkpoint fields,
missing durable plans and exhausted retries block instead of resetting progress.

```nix
services.freya-node = {
  enable = true;
  # Retain existing user/group, configFile, externalConfigFile and providerLedger.
  networkMode = "isolated";
  peerOperation = "follow";
  peerDiscoveryConfig = "/absolute/private/follow.json";
  continuousMode = false;
};
```

No service autostarts. Identity and private configs stay outside the Nix store.
`peer`, `acquire` and `follow` have separate persistent directories. The node
exposes no public RPC service. The packaged process disables Agave metrics export.

For explicitly authorized longer operation, set JSON `continuous: true` and module
`continuousMode = true`. Set `services.solana-budget-isolation.sessionSeconds`
explicitly (1–3600, default 55). The existing byte grant, namespace firewall,
shaping, billing-cycle checks and 120 GB normal / 150 GB hard monthly accounting
still apply. No session or billing cycle renews automatically. Continuous means
following successive windows **within the current authorization**; lease expiry,
quota exhaustion or denial stops it. Waiting for the next finalized slot no longer
counts as a planning failure. A new session requires explicit operator action.

Default diagnostic traffic cap is at most 1 MB; continuous config permits at most
10 MB, still subordinate to the already authorized kernel grant. No default rate
or grant is raised. Large cluster metadata and complete ledger acquisition may
exceed the available budget. Do not treat configured caps as measured capacity.

## Resource controls and recovery

The process/service retains 2 GiB memory containment, no swap, 128 tasks/descriptors,
128-batch gossip queues, a 512-identity CRDS trimming target and at most 64 repair
candidates. CRDS trimming is periodic; a larger observed table blocks the session.
Shred indices, four-slot windows, retry counts and disk usage are bounded.
Temporary handoff writes reserve disk headroom. Nothing silently prunes unresolved
evidence; storage exhaustion blocks collection. Retention is operator archival
rather than automatic deletion. The complete OS/bot/ML/node working set has not
been measured against 64 GiB.

Status is saved JSON, not a live RPC service. Check timestamps. It distinguishes
RPC-reported height/age/lag, local reconstruction, missing indices, attempts,
peers, native qualification, receipt dependency, disk bytes and Linux RSS where
available. Namespace rx/tx counters are actual aggregate traffic, including
planning/other workloads; exclusive gossip/repair payload and CPU measurements
remain unavailable. A stale head does not establish current lag.

## Operator validation and rollback

On Monolith, enter the dedicated environment:

```bash
nix develop --no-write-lock-file .#native-mainnet
freya-mainnet-preflight
```

Preflight automatically detects the active, lowest-metric IPv4 default-route
interface and reports existing service identity, namespace and storage mappings.
Equal-priority routes fail instead of guessing. It emits JSON and performs no
connectivity probe, authorization or network changes. Its detected uplink is a
candidate for the host's existing NixOS configuration; it does not rewrite it.
Host discovery success alone does not validate credentials, ledger billing dates
or trust permissions. A ping cannot discover those. Run the bounded service
diagnostic only after the full configuration/budget checks above pass.

This x86_64 Linux shell includes the packaged `freya-node`, `freya-peer`, Rust
HTTP/WebSocket Python extension, RPC diagnostics, SQLite, JSON and namespace/
firewall tools. It inherits `native-rpc`; no pip installation or manual planner
environment is needed for the packaged node. Entering it does not activate a
service, create a network grant or run a mainnet probe. Nix may build dependencies
and run their offline package checks when the operator enters the environment.

For peer acquisition, apply the reviewed NixOS configuration above first. Then,
from this environment, the operator starts `freya-peer-discovery.service`; systemd
supplies the private configs, shared ledger and authorized network namespace.
Running `freya-peer follow` directly in the host shell still fails closed without
that namespace/lease. Merely entering a development shell does not grant access.

```bash
nix build --no-write-lock-file .#native-peer .#native-node .#bundle
nix develop .#native-rpc --command python validate_production.py --native --output /tmp/freya-validation-NEW.json
nix build --no-write-lock-file .#checks.x86_64-linux.solana-network-lifecycle
sudo nixos-rebuild build --flake /absolute/path/to/system-flake#monolith
sudo nixos-rebuild switch --flake /absolute/path/to/system-flake#monolith
sudo systemctl start freya-peer-discovery.service
sudo journalctl -u freya-peer-discovery.service --no-pager -n 30
nix run --no-write-lock-file .#node -- --peer status /dev/null /var/lib/freya-node/follow
```

Run status as the collector UID. Activation consumes the explicitly authorized
session. Do not restart an active budget controller to obtain a replacement grant.
Verify native handoffs before enabling strict index reconciliation. Use existing
paper entrypoints only after complete finalized windows/oracle/market evidence
become available. Never force entries or labels to obtain acceptance.

Rollback: stop `freya-peer-discovery.service`, restore the previous reviewed NixOS
generation/config and remove optional `native_handoff` to retain external-only
indexing. Preserve identities, plans, raw/conflicting shreds, model/wallet/trade
state, index checkpoints and both provider/network ledgers.

`flake.lock` and Cargo locks are tracked. The existing Agave/nixpkgs/overlay pins
were retained and contain public repository identifiers/hashes only. Normal
updates: review a proposed revision, change only its explicit input pin if needed,
run `nix flake update INPUT_NAME`, inspect the lock diff and rebuild/test before
committing. Do not remove/recreate locks or upgrade unrelated inputs for freshness.

## Acceptance evidence

Final protected offline suite: **659 tests passed**, no failures/skips, 56.046 s.
All **nine Rust engine fixtures passed**, covering reconstruction/parity recovery,
ping/pong, response rejection, private state and durable restart. `native-peer`,
`native-node` and `bundle` built successfully with the committed dependency pins.
The launcher crate also compiled; its own test target contains zero tests.

The test/fix cycle corrected pooled response release, private temporary-directory
setup, effective UID lookup and pruned-history skip accounting. The actual packaged
CLI rejected unauthorized following, durably reported `blocked/network_authorization`
and returned read-only status successfully. Peak child RSS was **15,020 KiB for
offline startup/status only**, not synchronization or whole-system memory.

The final local harness report is `/tmp/freya-native-complete-offline-20261103.json`.
It retains `complete:false` and `live_ready:false`: runtime acceptance is separate.
NixOS lifecycle VM-test derivation evaluation succeeded, but no KVM device is
exposed here and the VM was not booted.

No mainnet peer, Solana provider or trading service was started during this
acceptance pass: this host has no active authorized node-network lease. Fixture
results do not establish mainnet synchronization. Real repair acceptance, sustained
catch-up, usable native-reconciled paper observations, direct parity availability
and full-system resource use remain operator acceptance gates. Use the bounded
Monolith commands above; preserve all caps and evidence on failure.

The subsequent `native-mainnet` shell and automatic-bootstrap changes were prepared
without builds, test execution or network diagnostics at the operator's request.
The acceptance counts above describe the preceding revision. Bootstrap-selection
regressions are written but await operator execution.
