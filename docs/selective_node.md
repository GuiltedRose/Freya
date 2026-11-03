# Selective node: initial, unvalidated implementation

No tests, builds, benchmarks, RPC calls or services were run for this change.
This is an initial foundation, not acceptance or independent consensus validation.
The existing V3 pipeline and its operational state are not replaced or activated.

## Implementation and limits

`native/freya_node` is a dependency-free Rust entrypoint. It replaces itself with
the bundled coordinator, stripping known signer environment variables. Actual
HTTP/WebSocket I/O uses the existing Rust extension; Python retains decoder,
SQLite, checkpoint and candle ownership. It is **not** a new Rust execution engine.

The real acquisition path reuses `solana_observer` and `onchain_stream`: two
program-specific, connection-reused subscriptions supply provisional hints;
existing finalized full-block reconciliation supplies authoritative coverage
under the existing source-trust policy. No per-notification transaction polling,
new cryptography, P2P program-filter assumption or second market decoder is added.
This does not yet eliminate full-block traffic or upstream dependence.

| Mode | Current behavior |
| --- | --- |
| external | Explicit reviewed Rust HTTP/WSS source; no default connection |
| automatic | Same external acquisition, with visible native-unqualified fallback reason |
| native | Fails closed; qualified local producer/handoff is not implemented |

Native qualification cannot be enabled by a boolean, training result or model
promotion. Dynamic native/external handoff, independent finality, account
subscriptions and source-specific retention remain unfinished. The existing Agave
package/module is retained as a **legacy replay implementation**, not silently
replaced by a filtered collector. New deployments have a separate `native-node`
package/module. The `bundle` output includes both the current app and entrypoint.

## Minimum data dependency map

| Trading observation | Source | Verification/admission | Retained evidence |
| --- | --- | --- | --- |
| Creation, migration, curve/pool trades and liquidity | Pump/PumpSwap mentions for discovery; complete finalized receipts for truth | Existing pinned decoder and mint/pool/route bindings; success/error and canonical order | Original relevant receipts, block manifests, source/schema IDs |
| Nested/CPI routes and wrapped SOL | Full messages, loaded addresses, inner instructions, logs, token/SOL pre/post balances | Existing route reconstruction, intermediate-leg exclusions and actual balance movement | Exact transaction and instruction indexes, balances and logs |
| Token-2022 and quote fees | Account/token extension and checked-transfer evidence | Unknown extensions/fees remain unknown or quarantined, never assumed zero | Original account/receipt data and decoder provenance |
| Reserves, prices, volume and candles | Admitted swaps plus required account state and complete slot ranges | Existing freshness, liquidity, coverage and cost gates | Ordered events, required account versions, candle inputs |
| USD conversion | Reviewed oracle account updates with historical publish times | Causal time/slot/confidence checks; no current-price backfill | Versioned original oracle observations |
| Saved-wallet context | Indexed relevant receipts and existing wallet parser | No transfer-as-trade or wallet-count-as-human inference | Receipt-backed observations and eligibility provenance |
| Finality, forks and missing slots | Existing finalized RPC head/manifests and full-block reconciliation | Upstream-claimed finality remains explicit; subscriptions do not prove omissions absent | Durable ranges, gaps, checkpoint and original block evidence |
| Paper outcomes/learning | Existing admitted market pipeline and genuine closes | Existing source rights, labels, qualification and reset rules | Existing outcome/model journals, never new manufactured labels |

A slot manifest/count or agreement between providers is not a cryptographic
omission proof. Quiet subscriptions never complete a candle. Unsupported receipts
remain blocked. Without adequate authenticated omission proofs, full-block
reconciliation is retained. No claim of verified global state is introduced.

## Resource, storage and status contract

A dedicated marked directory refuses pre-existing unowned data. Original observer
transactions/blocks and finalized checkpoints remain in its SQLite index. Writer
locking prevents duplicate ownership. Nothing prunes unresolved evidence or any
existing trading/model data. The disk limit stops collection; it does not delete
history. Recovery resumes the same marked directory/checkpoint.

Existing WS messages/queues, hint counts, response/decompression bounds and RPC
concurrency remain in force. The default worker virtual-memory ceiling is 4 GiB;
NixOS also applies a 4 GiB resident cgroup maximum and disables swap for this
service. These are containment limits, **not measured memory fit**, and do not
certify the complete 64 GiB system. Existing source network modes, isolated-node
caps and shared external-provider pacing remain mandatory. A configured shared
ledger mismatch is rejected rather than creating a separate rate allowance.

`status.json` is atomically published, owner-only, headless and read-only through
`--status`. It exposes source/fallback/trust, checkpoint/lag, coverage, peak process
memory, CPU time, disk use and the existing provider-ledger path. Unmeasured
protocol/retransmission/relevant/irrelevant bytes and event-efficiency metrics are
`null`, not invented zeroes. GUI, independent whole-system accounting, detailed
traffic classification and dynamic local qualification are future work. Source
configuration errors expose their type without authenticated URLs.

## Operator prerequisites and commands

Do not run beside active V3 collection on the shared connection. First review the
new files and configuration. An observer source needs the existing pinned schema
and deployment ranges, authorized Rust HTTP/WSS endpoints, storage/stream rights,
reviewed oracle configuration and 1–32 explicit mint/pool candle markets. Reuse a
protected source configuration; never copy credentials into the repository/Nix
store. This worker deliberately does not load a wallet `.env`.

Include new public files in the Git source before Git-backed flake builds:

```bash
git add native/freya_node nix/native-node.nix nix/selective-node.nix \
  run_selective_node.py selective_node.example.json tests/test_selective_node.py \
  docs/selective_node.md
nix build --no-write-lock-file .#native-node .#bundle
nix develop --no-write-lock-file .#native-rpc --command python -m unittest discover -s tests -p test_selective_node.py
nix develop --no-write-lock-file .#native-rpc --command python validate_production.py --native --output /tmp/freya-selective-offline-new.json
```

All validation above is **operator-only and unexecuted**. After checks and source
review, configure an owner-only copy of `selective_node.example.json` outside Git.
Select the existing shared provider ledger with `CRYPTO_BOT_PROVIDER_LIMITS`.
With competing collectors stopped, a bounded operator-authorized acquisition is:

```bash
timeout --signal=INT --kill-after=30s 300s nix run --no-write-lock-file .#node -- \
  --config /absolute/protected/node.json --state-dir state/selective_node
nix run --no-write-lock-file .#node -- --status --state-dir state/selective_node
```

This is data acquisition only; it never starts live trading. Inspect actual
contiguous coverage, fresh causal conversions, recovery, pressure and shared
cooldowns before any integration with production market consumers. The new index
is separate from V3 and is not automatically admitted to training or trading.

Optional NixOS integration: import `self.nixosModules.native-node`, explicitly set
`services.freya-node.enable`, protected `configFile`/`externalConfigFile`, the same
`providerLedger`, an existing non-root collector `user`/`group`, and an empty
owned `stateDirectory`. Use the same UID permitted to write the existing ledger;
do not change active V3 file permissions to work around access failures. Choose
`networkMode` matching the source; isolated mode keeps the existing finite session
and manual network grant. The service has no `wantedBy` and no restart policy.
Build/review the host configuration before switching it; start
`freya-node.service` manually only after validation. Do not restart funded live
services as part of this work.

Rollback: stop only the owned collector/service, retain all node/provider/index/
model journals and markers, and rebuild the prior reviewed revision. Resume V3
through its unchanged operator workflow. Never restore older databases over newer
records or clear provider cooldowns. No data migration or reset is required.

## Phase 2: native gossip diagnostic (unvalidated)

`freya-node --peer` now selects a separate Rust engine, compiled inside the pinned
Agave workspace with its existing lock and Rust 1.97.1. It uses Agave's actual UDP
gossip service, signed CRDS validation, contact records and epoch-slot reports.
Python remains the compatibility coordinator for external mode. No Python runs in
the peer engine. Neither mode switches the bot's data source.

Discovery requires an active, root-owned, unexpired network-budget lease matching
the process network namespace. It cannot run in `remote_provider` mode. Existing
HTTP budgets and cooldowns are untouched. Bootstrap sockets must be explicit IPv4
addresses (1–4); the operator must obtain the current compatible shred version.
A shred-version match does **not** authenticate mainnet genesis or prove finality.
No bootstrap hostname, port, or shred version is silently guessed.

Create a protected JSON configuration outside the repository/Nix store:

```json
{
  "bootstrap_peers": ["REPLACE_WITH_REVIEWED_PUBLIC_IPV4:8001"],
  "shred_version": 0,
  "duration_seconds": 20,
  "max_peers": 64,
  "max_namespace_bytes": 250000
}
```

The placeholders deliberately fail validation. Duration is 1–50 seconds, peers
1–256 and namespace traffic 1–1,000,000 bytes. Use a new empty directory, owned by
the collector with mode 0700. A dedicated private identity is created there;
wallet keys are never read. Atomic fsynced JSON checkpoints retain session history,
last peer claims and interruption state. Previous peer addresses are evidence,
not automatically trusted restart/bootstrap inputs. Shred-version changes require
a separately owned state directory. Unknown/corrupt schemas fail closed.

Upstream queues are reduced to 128 batches; CRDS trimming targets 512 identities
(a periodic soft bound, not a hard allocation bound). The process has a 2 GiB
address-space limit, 128 descriptors and 128 per-UID processes; the service also
has a 2 GiB memory limit, no swap and 128 tasks. Peer snapshot overflow stops the
session. Resource limits are containment, **not measured capacity**. Namespace
rx/tx counters are actual aggregate interface traffic, including other consumers;
they are not exclusive gossip-byte measurements. The kernel budget remains the
hard traffic backstop; application limits are sampled every 100 ms and may
overshoot. Unsupported protocol counters remain null.

Peer identities and slot claims are quarantined in `peer/checkpoint.json`.
`observed_network_slot` is only a compatible peer's advertised epoch-slot claim,
not local acquisition, a root, a finalized checkpoint or verified execution.
Recovered ranges remain empty; missing ranges remain unknown. CRDS handles signed
record conflicts, expiration and gossip retries; there is no ledger repair policy
yet. Discovery does not supply execution metadata, causal oracle history, complete
candles or training evidence. Native trading readiness always remains false.

### Monolith commands (operator only)

No compilation, tests, services or real networking were executed for Phase 2.
The following commands require the changed files to be Git-tracked and the existing
Nix inputs available. Build and offline verification first:

```bash
nix build --no-write-lock-file .#native-peer .#native-node .#bundle
nix build --no-write-lock-file --impure --expr \
  'let f = builtins.getFlake (toString ./.); in f.packages.x86_64-linux.native-peer.overrideAttrs (_: { doCheck = true; })'
nix develop .#native-rpc --command python -m unittest tests.test_selective_node
```

Review the existing V3 regression commands earlier in this document before
activation. For a bounded diagnostic, set these module options in the existing
collector configuration, retaining the current uplink, caps and service inventory:

```nix
services.freya-node.networkMode = "isolated";
services.freya-node.peerDiscoveryConfig = "/absolute/private/peer.json";
```

Prerequisites: existing `services.freya-node.enable`, reviewed collector UID,
private external configs/shared ledger and existing network-isolation configuration.
Do not stop or restart a running budget session merely to obtain a fresh grant.
No incoming port forwarding is added. The initial spy uses ephemeral outbound UDP
and receives replies; NAT/peer reachability may prevent discovery. Do not grant
extra ports or traffic without separate review.

```bash
sudo nixos-rebuild build --flake /absolute/path/to/system-flake#monolith
sudo nixos-rebuild switch --flake /absolute/path/to/system-flake#monolith
sudo systemctl start freya-peer-discovery.service
sudo journalctl -u freya-peer-discovery.service --no-pager -n 30
nix run --no-write-lock-file .#node -- --peer status /dev/null /var/lib/freya-node/peer
```

Read status as the collector UID if permissions require it. Status is a saved
snapshot; check `updated_at` and the service journal, especially after a crash.
SIGINT/SIGTERM request shutdown and join gossip workers; forced termination leaves
an interrupted checkpoint for the next session. Network authorization failure
records `blocked`; the root lease still closes networking if the process dies.

Rollback: `sudo systemctl stop freya-peer-discovery.service`, remove only
`peerDiscoveryConfig`, then rebuild/switch the previous reviewed configuration.
Keep the identity, checkpoints, unresolved claims and existing budget ledger.
External paper/indexer operation continues through its existing entrypoints;
never run competing index writers.

Gate A has source and packaging but awaits compilation. Gate B has actual gossip
code but awaits live discovery/resource validation and tighter internal cache
bounds. Gate C remains unimplemented: no shreds, repair, reconstructed blocks,
execution metadata or independent replay/finality. Gate D preserves separation,
but external regression compatibility is untested. Next milestone: demonstrate
bounded real gossip, then implement authenticated shred acquisition with durable
gap tracking before any market-data integration. No whole-system resource usage
has been measured.
