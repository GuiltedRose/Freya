# Phase 2: first authenticated native shred

## Evidence and current path

The operator's November 4 report for revision `5beaede` supplies matching
`planner_success`, `window_validated=true`, and window run ID
`000000000000000018e5059d6f69bd5c`. Planning took 29.452 seconds. Slots
465539000–465539003 and shred version 50093 are reviewed RPC claims.
The reported checkpoint has one repair candidate, one request attempt, one
timeout, no accepted responses/shreds, and an unchanged cursor. Those counters
are cumulative; this report alone cannot attribute every counter to this session.
Highest demonstrated milestone is validated RPC planning; persisted native state
also evidences candidate selection and a repair attempt, not successful delivery.

`native/freya_node/src/follow.rs` selects up to 64 recent gossip contacts with the
matching shred version, excluding self. It rotates by bounded attempt count among
advertised UDP serve-repair endpoints. This does not prove the peer retains the
slot or will serve an unstaked requester. It uses the pinned Agave
`RepairRequestHeader` and `ServeRepair::repair_proto_to_bytes` to sign a
`HighestWindowIndex` request until the terminal index is known, then `WindowIndex`
for the first missing index. The header binds sender, recipient, timestamp and a
random nonce. The request uses one persistent ephemeral UDP socket.

Responses must arrive from the selected exact address/port, fit the packet bound,
and carry the matching trailing nonce. `acquisition::checked` sanitizes the shred
and checks slot, version, index bounds, data type and expected leader signature.
An indexed request additionally checks its exact requested index. Authenticated
pings are answered only for the selected peer identity and endpoint, at most once
per attempt. The pinned server's `handle_requests` rejects a request while its ping
cache is unverified, even when it sends a ping challenge. Pong verification warms
that cache; it does not replay the discarded request. Existing bounded retries
remain necessary. No immediate retransmission has been added.

Accepted bytes are atomically published with file and directory fsync. Conflicting
bytes are retained and fail closed. Reconstruction revalidates persisted shreds,
parent/boundary consistency and supported parity before advancing the cursor.
Acquisition artifacts remain separate from execution receipts; `native_ready`
remains false.

## Confirmed corrections and uncertainty

Two static defects were confirmed and corrected without changing wire protocol:

1. Selecting from `all_peers` checked IPv4 only, bypassing the gossip library's
   global-address policy. Selection now applies that policy and rejects zero
   ports, unspecified/multicast/link-local/broadcast addresses.
2. A stop or follow deadline ending the receive wait was counted as a timeout.
   These outcomes now remain distinct and do not increment `repair_timeouts`.

Neither defect is established as the cause of the reported attempt. Missing
per-attempt diagnostics previously prevented distinguishing no UDP arrival from
rejected responses or a ping-only exchange. `checkpoint.json` now retains one
bounded `last_repair` record with session ID, endpoint/socket, nonce, request kind,
kernel send acceptance, expected/unexpected datagram counts, oversized/rejected
packet counts, authenticated pings, pongs sent, timestamps and outcome.
`send_confirmed` means the local kernel accepted the datagram, not wire delivery.
A count of rejected packets does not isolate nonce, signature or version failure.
The record is replaced per attempt, not an unbounded packet log. Existing counters
and all persistent claims are preserved. Abrupt termination can leave the last
published outcome `prepared`/`waiting`; it must not be promoted to success.

Supported causes still include peer overload/stake policy/nonresponse, missing
historical shreds, stale advertised endpoints, UDP loss or namespace/NAT/filter
issues, ping handshake without a subsequent bounded request, incompatible wire
protocol or clock skew, rejected nonce/socket/shred evidence, short receive time,
and lease/stop termination. One timeout proves none of these individually.
The selected endpoint and local socket from the reported old attempt were not
provided, so its exact transmission and timeout cause remain unresolved.

## Validation and deployment

Three new Rust regressions passed in an isolated harness using the exact production
helpers/tests and pinned `SocketAddrSpace` source: endpoint policy, wait outcomes,
and preservation of cumulative evidence. `git diff --check` passed. This is not a
full engine build, cryptographic integration test or mainnet acceptance.
The offline Nix build was attempted but the daemon socket returned `Connection
refused`; complete native-peer/native-node verification remains outstanding.

On Monolith, with Nix available, run:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
```

The peer derivation runs the complete Rust engine suite, including existing
signature/nonce/socket/version, ping/pong, reconstruction, checkpoint and lock
fixtures plus the new regressions. If dependencies are not already cached, resolve
the build prerequisites through the operator's normal build workflow. No service
activation occurs from this command. Do not treat the helper-only results as a
replacement for this build.

Apply changes only through the existing refresh, diff review, backup, build,
dry-activate and test workflow in
[native_network_investigation.md](native_network_investigation.md). Verify both
installed node and peer references; `follow.rs` is compiled into the peer engine.
Use that document's existing stop/restore/test rollback procedure, retaining all
checkpoints, raw shreds, identities and ledgers. Restart remains `no`; the lease
remains 55 seconds, with no automatic renewal or diagnostic runs.

## Next diagnostic: operator authorization required

First inspect the previous invocation with
[native_shutdown_review_20261104.md](native_shutdown_review_20261104.md). After
build/deployment validation, confirm both services inactive and record a private
before snapshot. Obtain explicit operator authorization for one existing capped
session before running the start command below. Do not automate it or retry it.

```bash
sudo systemctl is-active freya-peer-discovery.service solana-budget-network.service
sudo jq '{updated_at,repair_session_id,window_start,next_slot,repair_requests,
  repair_responses,repair_timeouts,validated_shreds,last_reconstructed_slot,last_repair}' \
  /var/lib/freya-node/follow/checkpoint.json
# ONLY after explicit authorization for this single session:
sudo systemctl start freya-peer-discovery.service
```

After bounded shutdown, repeat the jq snapshot and read the matching planning
records and both journals using the previous review's read-only commands. Match
session/run IDs, timestamps and before/after counters. Inspect the recorded
slot's `.shred` file metadata privately; do not expose identity keys. If the record
shows only silence, use the existing operator-controlled, interface-correlated
capture procedure in the investigation runbook in a separately authorized session
before changing networking. Never clear cooldowns or reset state to get a retry.

Phase 2A requires an authenticated, nonce/socket-correlated valid shred, its durable
raw file after shutdown, and matching current-session diagnostics. No such shred
has yet been demonstrated. Phase 2B requires enough revalidated data for ordered
reconstruction and safe cursor advancement; neither phase establishes consensus
replay or trading readiness.
