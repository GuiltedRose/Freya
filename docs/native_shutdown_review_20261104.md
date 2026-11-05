# November 4 bounded-session shutdown review

Reviewed checkout: `5beaede2a0bf3432af94095e4ab23d77f9c34463`, initially clean.
This is a static investigation plus the operator's supplied journal excerpts.
Monolith's system bus is inaccessible here; `/etc/nixos`, follower state and
lease records are unavailable. No tests, builds, service starts or network probes
were performed. The deployed store paths are **not independently verified**.

## Findings and limits

The 21:37:25 excerpt shows a systemd stop job and successful deactivation. It does
not identify who requested the stop. The earlier exit status 1 was a different,
failed invocation; neither its failure nor the previous lock issue explains the
latest run without matching diagnostics.

The reviewed design intentionally bounds sessions:

- `nix/solana-network-isolation.nix`: peer Requires/BindsTo/After the controller;
  both have `Restart=no`. Controller exits on expiry, grant exhaustion, signal,
  or error. Its disappearance stops the bound peer.
- `solana_network_lifecycle.py:start`: the 55-second lease begins after the grant
  but **before** namespace/firewall/shaping setup and READY notification. Peer
  execution therefore gets less than 55 seconds. A 45.539-second lifetime is
  compatible with setup using the other seconds; this is a hypothesis, not a
  measured breakdown. Monthly cycle end can also shorten the lease.
- `nix/selective-node.nix`: peer `RuntimeMaxSec=infinity`, stop timeout 3 seconds,
  no automatic restart. Both modules also permit configuration changes to stop
  units (`stopIfChanged=true`). Restart policy alone cannot trigger a stop.
- `prepare_mainnet_host.py`: generated noncontinuous follow duration is 40 seconds.
  `follow.rs` starts that timer after initial planning/bootstrap checks. Clean
  completion or signal handling can leave unresolved work; no readiness follows.

**Highest verified application stage:** none for the latest invocation. Traffic
and TLS records establish neither validated RPC metadata nor native progress.
No new application or deployment defect is confirmed. Do not change lease,
provider pacing, retries, restart policy or networking based on this excerpt.
Existing controller events record start/stop and grant ID, but not a distinct
stop reason; a stop event alone cannot distinguish expiry, quota and signal.

## Read-only evidence collection on Monolith

Run these in the operator's existing shell, without starting a service. Keep raw
unit/config output private; inspect locally for credentials before sharing.
Times below are interpreted in Monolith's timezone, matching its journal excerpt.
Check timezone first; do not reinterpret the timestamp as New York or UTC without
confirming the host clock. Expand the interval if the controller started earlier.

```bash
date --iso-8601=seconds
timedatectl show -p Timezone
git rev-parse HEAD
git status --short
sudo systemctl show freya-peer-discovery.service solana-budget-network.service \
  -p InvocationID -p Restart -p BindsTo -p Requires -p After \
  -p RuntimeMaxUSec -p TimeoutStartUSec -p TimeoutStopUSec \
  -p Result -p ExecMainCode -p ExecMainStatus -p ExecStart \
  -p ActiveEnterTimestamp -p InactiveEnterTimestamp -p ExecMainStartTimestamp -p ExecMainExitTimestamp
sudo systemctl cat freya-peer-discovery.service solana-budget-network.service
sudo journalctl -u freya-peer-discovery.service -u solana-budget-network.service \
  --since '2026-11-04 21:35:30' --until '2026-11-04 21:38:00' -o short-precise --no-pager
sudo ls -lah /var/lib/freya-node/follow/
for name in planning-supervisor planning-progress planning-error; do
  sudo cat "/var/lib/freya-node/follow/$name.json"
done
sudo jq '{format,state,updated_at,last_error_category,native_ready,continuous,
  rpc_source_identity,window_start,next_slot,observed_network_slot,
  peer_connected,repair_requests,repair_responses,repair_timeouts,validated_shreds,
  last_reconstructed_slot,queue_in_flight,attempts}' \
  /var/lib/freya-node/follow/checkpoint.json
sudo jq '{schema,planning_run_id,at,start,end,source_identity,verification,
  shred_version,shred_version_at,slots,rpc_reported_skipped_slots}' \
  /var/lib/freya-node/follow/window.json
```

`systemctl show` describes current/latest state, not necessarily the historical
invocation. Match journal invocation IDs and diagnostic timestamps. A later
attempt may have replaced diagnostics. Do not read or share `identity.json`.
Missing files are evidence gaps, not permission to recreate/reset state.

From controller ExecStart, identify its installed `--config` store file; inspect
only `sessionSeconds`, `sessionBytes`, and `ledger` with `jq`. Using that ledger's
actual path, read sibling `network-events.jsonl` and correlate its grant ID and
epoch timestamps. `/run/solana-budget-status.json` is removed during cleanup;
its absence after shutdown is expected. With the configured ledger path:

```bash
budget_ledger=/var/lib/solana-network/budget.sqlite # replace if installed policy differs
sudo cat "$(dirname "$budget_ledger")/network-events.jsonl"
sudo sqlite3 -readonly "$budget_ledger" \
  'SELECT id,bytes,created,rx,tx FROM grants ORDER BY created DESC LIMIT 5;'
```

A near-expiry controller stop supports lease expiry; `rx+tx >= bytes` supports
quota exhaustion. A controller error, signal, or configuration activation needs
its journal evidence. Below-quota counters alone do not establish expiry.

## Deployment and progress checks

Inspect `/etc/nixos/freya-mainnet.nix` locally for its SOURCE and PACKAGE store
paths. Compare the installed scripts/wrappers reached by active ExecStart with
the generated add-on; follow `FREYA_NODE_PEER` to the actual peer engine. Building
a package does not change installed paths. For an unchanged clean checkout,
these offline evaluations provide expected source/package references:

```bash
nix flake metadata --offline --no-write-lock-file --json . | jq '{revision,path}'
nix eval --offline --no-write-lock-file --raw .#packages.x86_64-linux.native-node.outPath
nix eval --offline --no-write-lock-file --raw .#packages.x86_64-linux.native-peer.outPath
```

A different path requires examining the derivation/source; a version string alone
cannot prove a revision. Do not refresh/activate merely to collect this evidence.

Match supervisor/progress/error run IDs to `window.planning_run_id`. A current
`planner_success` with `window_validated=true` proves planning, not gossip.
A persisted window can come from an earlier run or be intentionally reused.
`peer_connected > 0` supports usable gossip repair candidates; rising
`repair_requests`, `repair_responses`, and `validated_shreds` support later stages.
These counters and `last_reconstructed_slot` persist across runs: attribute them
to this session only with before/after evidence. Reconstruction still does not
establish consensus replay, trading readiness or profitability.

## Corrections, verification and next session

No implementation change or new regression test is justified until this evidence
identifies a defect. Existing focused offline checks, for the operator to run:

```bash
nix develop .#native-mainnet
python -m unittest tests.test_solana_network_lifecycle tests.test_native_window_planning
nix build --no-write-lock-file .#native-peer .#native-node
```

The peer build runs its Rust supervisor, checkpoint and lock regressions. If shell
construction fails, run the Nix build directly first; a missing Python command
does not diagnose the application. These checks do not activate services.

After reviewing the existing run, obtain explicit authorization before another
capped session. Record pre-session diagnostic/counter timestamps privately,
confirm both units inactive and the installed paths, and wait out any existing
provider cooldown without clearing it. Only then authorize one manual
`sudo systemctl start freya-peer-discovery.service`, wait for its bounded exit,
and repeat the read-only collection. Do not automate retries or renew the lease.
Use the existing refresh/review/backup/rollback workflow in
[native_network_investigation.md](native_network_investigation.md) only if a
confirmed deployment mismatch actually requires correction. Preserve all state.
