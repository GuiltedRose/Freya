# Self-hosted RPC infrastructure; not activated

The flake now exports `nixosModules.selfhost-rpc` and `lib.mkAgavePackage`.
This is a real non-voting Agave replay-node service, separate from the Rust RPC
client/indexer. The pinned Agave package has now built on Arch through Nix, and
its executable/version/argument checks passed. A temporary isolated local test
validator reached RPC health `ok` and was stopped. Production-validator startup
was blocked by this host's OS limits; no mainnet bootstrap was performed.

The service follows the official [Agave RPC setup](https://docs.anza.xyz/operations/setup-an-rpc-node):
non-voting, private RPC advertisement, full RPC operations and transaction history.
RPC binds to **127.0.0.1:8899 inside the existing isolated namespace**, with WS on
8900. Indexer, paper and live services in that namespace can consume it locally.
This does not expose RPC to the host/public internet or restore Pump collectors.

## Transfer policy

This is a **self-hosted-node policy for bandwidth-limited connections**, including
home connections. It is not an allowance imposed by Solana's remote RPC service
and is not a requirement for remote-only collection, paper trading or learning.
The shipped conservative profile below is useful to other low-bandwidth operators;
it does not establish that their connection can sustain mainnet replay.

* **120 GB normal collection allowance** per explicit billing cycle.
* **150 GB absolute policy ceiling**, decimal: 150,000,000,000 bytes, total RX+TX.
* The existing 5 GB safety margin remains: reserve-authorized operational grants
  stop at 145 GB of debited IP traffic, before the 150 GB policy ceiling.
* Above 120 GB, essential reserve use still needs explicit authorization. No
  automatic reserve release, cycle rollover, accounting reset or session renewal.
* The node joins the same fail-closed quota/shaping namespace. Gossip, bootstrap,
  snapshots, replay recovery and retransmissions count; in-namespace loopback RPC
  does not consume WAN transfer. Optional bounded peer-port DNAT also passes
  through the namespace's shared counters/quota.
* Kernel lease expiry/quota exhaustion closes traffic; service binding stops the
  node when the network session ends. A controller failure cannot leave an
  unlimited networking session. Existing short sessions/rate limits are unchanged.

Host IP accounting is not an ISP wire-meter guarantee: incoming packets can cost
transfer before a host firewall drops them. An upstream/provider-enforced transfer
cap is required if **150 GB must be guaranteed at the ISP billing meter**, including
unsolicited traffic. The existing report continues to declare that limitation.
Do not call local cutoff settings proof that the ISP can never bill an overage.

### Choosing a mode on a limited connection

| Mode | Traffic and enforcement |
| --- | --- |
| Remote provider | Requests/responses to an existing RPC service; retain provider pacing, backoff and source permissions. No self-hosted-node billing ledger is required. |
| Self-hosted node | Includes bootstrap, snapshots, gossip, repair and ongoing replay. Use the isolated quota/lease controller and real billing-cycle accounting. |
| Hybrid | The node retains its full budget policy. Consumers in its shared namespace also share its WAN quota; loopback RPC is local. Remote endpoints still have independent provider limits. |

For remote Rust transport, explicitly set endpoint `network_mode` to
`remote_provider`, `source_kind` to `solana_rpc`, and supply reviewed policy
`remote_provider_verified: true` alongside existing storage rights/evidence.
Use HTTPS and same-host WSS. Local/private literal addresses, local-node source
kinds and conflicting node budgets are rejected. Omitting the mode retains the
isolated-node boundary checks; existing node configurations are not exempted.
`solana_observer.example.json` is a disabled remote template; its permission flags
remain false until reviewed. The node templates keep their existing restrictions.

The Nix paper module's `rpcMode = "remote"` does not attach its processes to the
self-hosted-node budget service. Enabling the node module still enables enforcement
for the node itself. Nix changes remain untested here.

Before choosing self-hosting, account for the ISP's **actual** cycle dates and any
other household traffic sharing its cap. The node ledger measures granted node
traffic, not unrelated devices; budget less for the node if the ISP cap covers the
whole home. Do not initialize accounting as zero partway through a cycle without
establishing prior use. Compression trades CPU/I/O for smaller snapshot archives;
it cannot guarantee that bootstrap or ongoing replay will fit a 150 GB allowance.
Use a remote provider when the connection cannot sustain the node, instead of
disabling cutoffs, automatically releasing reserves or repeatedly restarting it.

Solana documents the public mainnet endpoint and its
[provider limits](https://solana.com/docs/references/clusters), and explicitly does
not recommend public endpoints for production applications. It can be considered
for bounded supervised evaluation; a maintained production deployment needs an
appropriately provisioned, permitted RPC service. A remote endpoint is not proof
of sufficient finalized history, current observations or oracle coverage.

## Declarative host configuration

Import the bot flake's `nixosModules.selfhost-rpc` alongside its bot module. Supply
the default Agave package or an audited compatible override; no mutable release
installer runs during activation. The factory builds validator and keygen binaries:

```nix
agavePackage = cryptoBot.lib.mkAgavePackage {
  inherit pkgs;
  src = inputs.agave-source; # Operator-reviewed immutable revision, flake = false.
  version = reviewedAgaveVersion;
  cargoHash = reviewedAgaveVendorHash;
  rustPlatform = reviewedRustPlatform;
};
```

The default package pins [Agave v4.3.0](https://github.com/anza-xyz/agave/releases/tag/v4.3.0)
at `825efd18292aff6ffcf9daa0f7612f21b3531a72`, uses its Cargo.lock, and selects
Rust 1.97.1 from its [toolchain declaration](https://github.com/anza-xyz/agave/blob/v4.3.0/rust-toolchain.toml)
through rust-overlay. No dependency hashes are invented: Cargo's checksums and
immutable locked Git revisions are used when no vendor hash is supplied. The
operator must generate and retain `flake.lock` before deployment; the overlay input
is not reproducibly pinned until that lock is recorded. Review the official cluster
[version floor](https://github.com/anza-xyz/agave/wiki/feature-gate-tracker-schedule)
before building a release. The package and its native dependencies remain
**built and checked at CLI/local-fixture level only**. The NixOS service still needs
operator activation testing. Other systems can use remote RPC; the packaged
Agave node currently targets x86_64-linux.

Configure `services.crypto-bot-node` with `enable`, that `package`, external
`identityFile`, dedicated persistent `stateDirectory`, reviewed mainnet
`entrypoints` and `knownValidators`. The identity is a separate node key, not the
trading-wallet signer. It is loaded through systemd credentials, never embedded in
the Nix store. No keys are generated automatically. Agave's native snapshot
retention removes superseded snapshot archives when the operator runs the node;
this implementation never executes cleanup during setup or deployment.

## Safe retention

The [upstream snapshot guide](https://github.com/anza-xyz/agave/wiki/Snapshot-Guide)
distinguishes replay snapshots from historical blocks. Incremental snapshots
depend on a matching full snapshot. Use Agave's native archive retention, never a
cron job deleting files by age. The module passes the documented
[snapshot retention flags](https://github.com/anza-xyz/agave/blob/v4.3.0/validator/src/commands/run/args.rs)
with explicit defaults:

```nix
services.crypto-bot-node.retention = {
  fullSnapshots = 3;        # Allowed range: 2..1024.
  incrementalSnapshots = 8; # Allowed range: 4..65536.
};
```

These are archive counts, not days of transaction-history coverage. Superseded
archives may be discarded by the validator; retain additional external recovery
archives when rollback requires them. The validator manages archive dependencies;
do not independently delete account files, live RocksDB files or snapshot bases.

Old block data can also be expendable, but its age alone does not establish that.
No `--limit-ledger-size` flag or external block-deletion job is installed: that flag
limits shreds rather than guaranteeing a minimum history window. Before enabling
ledger pruning in a future policy, verify that the retained block range covers
every indexer checkpoint and pending-order reconciliation requirement, and that
older required history is already durably indexed or available from an explicitly
permitted archive. A downloaded snapshot does not supply old transaction history.
Missing coverage must stop or report the affected consumer, never silently skip
forward or reset its checkpoint. Until that coverage guard exists, keep block
history and provision sufficient storage rather than promise safe automatic
ledger pruning.

This cleanup permission applies only to disposable node blockchain artifacts.
Wallet records, transaction journals, indexed market history, training evidence,
models, checkpoints and bandwidth accounting remain protected. Reverting a NixOS
generation cannot restore archives already removed by retention.

## Compression, memory and deployment choices

Snapshots use Zstandard level 15 (configurable 1–19). Ledger transaction-status
compression uses zlib, one of the [supported ledger codecs](https://github.com/anza-xyz/agave/blob/v4.3.0/validator/src/commands/run/args/blockstore_options.rs).
This does not compress all live account memory or guarantee a mainnet node fits a
small server. Native cache limits default to 1 GiB write / 1–2 GiB read, with a
25GB accounts index limit; memory pressure beyond the declared cgroup cap stops the
node rather than borrowing unrestricted host RAM. Higher-spec hosts should set
`memoryHigh`, `memoryMax`, `accountsIndexLimit`, `accountsWriteCache` and
`accountsReadCache` deliberately after operator measurements. CPU/disk cost rises
with heavier compression. XDP is disabled so node networking uses the existing
kernel namespace budget path.

The node storage guard checks each state/accounts/ledger/snapshots filesystem every
15 seconds. Below `storageFreeMinimumBytes` (default 100 GiB), its failure stops the
bound node. This is a reserve alarm, not a guaranteed write-rate bound between
checks. It never erases history to recover space; investigate/provision storage
before manually restarting the node and its guard.

The CLI paper/live runners use a built-in self-hosted profile without setup
questions. Literal loopback/private addresses are detected automatically; for an
owned node at a DNS hostname set `CRYPTO_BOT_RPC_MODE=selfhosted` in `.env`.
The namespace and bandwidth status checks still apply before RPC access; setting
this mode never creates or resets a bandwidth ledger. See
[coordinated startup](coordinated_pipeline.md#first-configuration).

`services.crypto-bot-paper.rpcMode` selects `remote` (default), `selfhosted` or
`hybrid`. The offline configuration preflight requires the enabled endpoint list
to match: remote-only, literal-loopback-only, or both. Selfhosted/hybrid require
the enabled node module. Existing approved-source failover and rights checks still
apply; configuring a mode never approves a source. Keep the oracle's source ID
consistent with the enabled list. Live transaction submission uses its separate
reviewed `SOLANA_RPC_URL` runtime environment; explicitly select the desired local
or remote endpoint there too. This module does not silently replace that setting.
Do not expose the private node publicly to connect a second machine; a separate
authenticated transport and budget/security review would be needed.

The mainnet genesis is pinned to the same value as `wallet.py`. Capacity review is
explicit (`capacityReviewed = true` only after operator review). Memory controls
default to 48G high / 56G maximum with no swap; these protect host headroom and do
not assert that the declared 64 GB monolith can replay mainnet. State/snapshot disk
capacity, current release requirements and sustainable replay must still be checked.

The node registers its bounded peer range (default 8000–8026 TCP/UDP) with the
existing network controller. Private RPC/WS ports cannot be part of that range.
Upstream NAT/firewall reachability and the public endpoint remain operator
prerequisites. The module does not weaken Agave port checks or discard corrupt WAL.

### OS prerequisites found during the startup check

The pinned Agave CLI requires a dynamic-port range spanning at least 26 ports;
the original 8000–8020 default was rejected. The module now defaults to 8000–8026
and rejects smaller spans. The package also declares `agave-validator` as its
main executable so `nix run .#agave-node` resolves the correct binary.

Agave's [OS checks](https://github.com/anza-xyz/agave/blob/v4.3.0/core/src/system_monitor_service.rs)
require socket-buffer maxima of at least 134217728 bytes and `vm.max_map_count`
of at least 1000000. The enabled NixOS node module now supplies these as
`boot.kernel.sysctl` defaults and sets a 2000000000-byte systemd memory-lock limit.
These source changes were not activated on this Arch host. Explicit host settings
can override the defaults; smaller values will still fail Agave's checks.

On a non-NixOS host, an administrator must review/apply equivalent OS settings
and launch the process with the required limits. Read current values first:

```bash
sysctl net.core.rmem_max net.core.wmem_max vm.max_map_count
ulimit -l
```

Do not add `--no-os-network-limits-test` just to obtain a passing startup. The
isolated smoke test observed a 4194304-byte receive-buffer maximum and an
8388608-byte memory-lock allowance. Raising them was not permitted here, so the
production executable correctly stopped. `net.core.netdev_max_backlog` was also
unavailable inside the private namespace; upstream treats that particular metric
as report-only, whereas insufficient recommended limits are startup failures.

Use `solana_observer.local.example.json` as the local collector template. Keep its
permission flags false until reviewed, set the enabled endpoint to literal loopback,
and retain deployment/schema/oracle review. Local RPC does not prove complete
current observations; downstream freshness, coverage and trading gates remain.

## Operator-only build, activation and rollback

After pinning the host package and configuration, on the suitably provisioned
NixOS node host (the current monolith is not the intended node host):

```bash
export NIXOS_FLAKE=/etc/nixos#rpc-host
nix flake lock
nix build .#agave-node
sudo nixos-rebuild build --flake "$NIXOS_FLAKE"
# Only after reviewing the build and all existing acceptance checks:
sudo nixos-rebuild switch --flake "$NIXOS_FLAKE"
sudo systemctl start solana-budget-network.service
sudo systemctl start crypto-bot-node.service
journalctl -u crypto-bot-node -u solana-budget-network -f
```

The protected billing ledger must already be initialized for the real cycle using
the existing network-budget procedure. This module does not recreate it. All units
are manual-start. Exhaustion or the existing 55-second session lease stops the node;
no automatic extension has been introduced. Such small bounded sessions/rate limits
are **not demonstrated sufficient for mainnet bootstrap or sustained replay**.
Defer runtime network validation until activation testing, but do not remove its
coverage from production acceptance: self-hosted nodes still exchange peer traffic.

To roll back, stop bot consumers first, then `crypto-bot-node` and the budget session,
and use `nixos-rebuild switch --rollback`. Retain node identity, accounts, ledger,
required snapshot chains and budget accounting. Never reset state or erase
accounting to retry. Keep external rollback archives outside the node's managed
snapshot directory if they must survive native archive retention.
