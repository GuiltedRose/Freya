# November 6 UDP/WireGuard review

Reviewed checkout: `424d6b1`, clean before this report. The supplied handoff is the
runtime evidence; no capture bytes, host journals or persisted peer records are
available here. No protocol/network changes, tests, builds or live sessions were
performed. No implementation defect explaining the timeout is confirmed.

## Main finding: a protocol-label collision is plausible

Pinned Agave `gossip/src/protocol.rs` defines `PingMessage` as variant 4 with a
32-byte token. `gossip/src/ping_pong.rs` serializes sender public key (32), token
(32) and signature (64). Including the four-byte enum tag gives a **132-byte gossip
ping starting `04 00 00 00`**. Its signature verifies the token with the embedded
sender public key. These are properties of the pinned source, not a decoding of
the supplied packet.

WireGuard transport also starts `04 00 00 00`, followed by a receiver index,
counter and authenticated ciphertext. Wireshark's current source accepts type 4
with zero reserved bytes and length >=32 in its heuristic; it does not establish
cryptographic validity. A gossip ping therefore meets that basic heuristic. Its
public-key bytes would be displayed as the supposed receiver index/counter;
repeated values could simply reflect a repeated Solana identity.

Sources: [WireGuard protocol](https://www.wireguard.com/protocol/),
[Wireshark dissector](https://raw.githubusercontent.com/wireshark/wireshark/master/epan/dissectors/packet-wireguard.c).
The operator's installed Wireshark version/profile must still be checked.
WireGuard's specification describes padded encrypted transport data; 132-byte
length alone is not proof of either protocol. Its WireGuard label has low evidential
value until bytes/handshake/authentication are examined.

Pinned **repair** ping is different: `core/src/repair/serve_repair.rs` defines
`RepairResponse::Ping` as variant 0, also 132 bytes, starting `00 00 00 00`.
Do not confuse it with gossip variant 4. Neither ping ends with a repair-request
nonce: its final four bytes belong to its signature. Thus `nonce_matches=false`
is not evidence that a 132-byte packet cannot be a Solana ping. Exact-source
rejection remains correct and is unchanged. No ping from another endpoint should
be admitted or answered by relaxing repair authentication.

## Timestamp discrepancy and earlier-request hypothesis

The supplied capture time, November 6 05:22:56.356695 EST, is epoch
**1793960576.356695**. The application sample is epoch **1793960582.6305578**,
or 05:23:02.630558 EST. Difference: **6.273863 seconds**. This is not a five-hour
timezone discrepancy.

`follow.rs` timestamps unexpected packets after `recv_from`, using wall-clock
`SystemTime`, not a kernel receive timestamp. It keeps the same repair socket
across requests and waits up to eight seconds between attempts. A packet can
arrive during a wait and remain queued until the next attempt; incoming shaping
can also delay delivery after an uplink capture. This is consistent with a late
packet from a previous peer, but does not prove this particular pair of records
is the same packet. Multiple identical-looking packets, capture-clock mismatch,
clock adjustments, capture interface and frame timestamps must also be checked.
Do not extend waits or remove cancellation/authorization checks on this basis.

## Peer relationships and expected transmission

- 208.91.111.195:8001: operator-reported configured Solana gossip bootstrap. The
  local checkout does not contain the deployed external peer metadata to verify
  its identity or ownership independently.
- 64.130.50.70:8008: recorded selected repair endpoint in the supplied latest
  attempt; production selection uses recent, same-version gossip advertisements.
- 185.8.106.229:8009: recorded unexpected UDP sender. No available record proves
  it was an earlier repair candidate or that it shares ownership with another IP.

No public infrastructure lookup is needed to infer ownership, and no such
inference is made. Different addresses participating in Solana do not establish
common ownership or actual WireGuard use. The reported absence of local WireGuard
interfaces does not identify the remote payload.

`send_confirmed=true` establishes local kernel acceptance only. Expected endpoint
64.130.50.70:8008 and socket 49458 need to be traced separately from gossip.
`expected_datagrams=0` means the application recorded no packet from that exact
endpoint during the last wait. It does not establish whether its request left
the uplink or whether any reply reached another capture point. No authenticated
shred, valid repair response or finalized reconstruction is yet evidenced.

## Read-only checks against existing captures

Keep captures/hex local and private. Set `cap` to an existing capture; these commands
make no network requests. Check the host timezone and tool/profile information:

```bash
date --iso-8601=seconds
timedatectl show -p Timezone -p NTPSynchronized
tshark --version
tshark -G heuristic-decodes | rg -i 'wireguard|wg'
tshark -G decodes | rg -i 'wireguard|wg'
cap=/absolute/path/to/existing-private-capture.pcapng
tshark -r "$cap" --disable-protocol wg \
  -Y 'frame.time_epoch >= 1793960566 && frame.time_epoch <= 1793960593 && ((udp.dstport == 49458) || (ip.addr == 64.130.50.70 && udp.port == 8008) || (ip.addr == 208.91.111.195 && udp.port == 8001) || icmp.type == 3)' \
  -T fields -e frame.number -e frame.time_epoch -e frame.interface_name \
  -e ip.src -e ip.dst -e udp.srcport -e udp.dstport -e udp.length
```

Identify every matching unexpected frame, not just the first. `udp.length`
includes the eight-byte UDP header. For one selected frame, inspect the UDP payload
locally; substitute its actual frame number:

```bash
tshark -r "$cap" --disable-protocol wg -Y 'frame.number == FRAME_NUMBER' \
  -T fields -e udp.payload
```

Check the tag and full 132-byte structure. In the existing `native-mainnet` shell,
one packet's hex can be checked offline for the pinned Solana ping signature:

```bash
# Put only that UDP payload's hex in a private file; no private key is needed.
python - /absolute/path/to/private-payload.hex <<'PY'
from pathlib import Path
import sys
from solders.pubkey import Pubkey
from solders.signature import Signature
p = bytes.fromhex(Path(sys.argv[1]).read_text().strip().replace(':', ''))
if len(p) != 132 or p[:4] not in (b'\x00'*4, b'\x04\x00\x00\x00'):
    raise SystemExit('Not the pinned 132-byte repair/gossip ping layout')
identity = Pubkey.from_bytes(p[4:36])
print('candidate:', 'repair ping' if p[:4] == b'\x00'*4 else 'gossip ping')
print('identity:', identity)
print('token signature valid:', Signature.from_bytes(p[68:132]).verify(identity, p[36:68]))
PY
```

This operator command has not been executed here. Signature verification identifies
a signed ping layout/key, not ownership of the source IP, an authenticated shred,
or permission to accept it on the repair path. Compare the public identity with
retained peer advertisements. If the candidate is not a verified ping, retain the
protocol classification as unknown; do not default to WireGuard.

Read the existing checkpoint/window projections for public identities and prior
records; never read/share `identity.json`:

```bash
sudo jq '{repair_session_id,bootstrap_nodes,bootstrap_peers,active_repair_peer,last_repair}' \
  /var/lib/freya-node/follow/checkpoint.json
sudo jq '{planning_run_id,at,bootstrap_nodes}' /var/lib/freya-node/follow/window.json
sudo journalctl -u freya-peer-discovery.service -u solana-budget-network.service \
  --since '2026-11-06 05:22:35' --until '2026-11-06 05:23:15' \
  -o short-precise --no-pager
```

Journal times assume the confirmed host clock is EST. A later attempt may have
replaced checkpoint/window records. Current diagnostics retain only the last
attempt, so use capture requests **before** the unexpected frame to check whether
185.8.106.229:8009 was previously targeted from the same socket. Matching tuple,
exact payload, and timestamp sequence are required to establish correlation.
A ping cannot be correlated by treating its signature bytes as a request nonce.

## Next step, only if existing evidence is insufficient

No production correction is justified by the WireGuard label alone. No regression
is added for an unconfirmed networking defect. Continue using the existing source,
nonce and signature rejection fixtures; operator offline build command remains:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
```

For one separately authorized bounded diagnostic, retain before/after checkpoint
projections and passive captures on the uplink **and** `solana-host` veth. Use a
60-second maximum capture, finite packet count and root-only output, following the
existing [capture/refresh/rollback runbook](native_network_investigation.md).
Captures may outlast the 55-second lease to observe cleanup; they do not renew it.
Do not start the service automatically. Record the actual new socket/target,
namespace `ss`, quota/drop counters and controller events. Correlate request
namespace egress → uplink egress → uplink reply → veth ingress → application sample.
NAT can change the public port. Capture all datagrams to the actual repair socket,
not just the expected remote, to retain late-peer evidence.

Preserve `Restart=no`, authorization, quotas, lease, checkpoints and trading
disablement. Success remains one genuine, source/nonce/signature-validated shred
persisted after shutdown; reconstruct and restore the paper feed only after the
required evidence passes.


## Follow-up: operator-verified repair ping

The newer handoff reports a 132-byte, tag-zero packet with a valid token signature
under public identity `8muBNZEu7w381b2Av8YRh15Ee2rHoqrqjRzk67pJ2mAW`.
This is consistent with pinned Agave `RepairResponse::Ping`; the reported
WireGuard label is incorrect for this verified packet. This is operator-supplied
cryptographic evidence; the raw payload is not available in this workspace.
It does not establish an address binding or authorization for the current request.

A targeted search of local `docs` and native source found no retained advertisement
for that identity. The IP appears only in the preceding investigation document.
Monolith's follower directory and `/tmp/freya-ping.hex` are unavailable here.
Consequently neither an earlier send to 185.8.106.229:8009 nor its advertised
identity can be established from available records. Exact-source rejection stays
unchanged, and the current 64.130.50.70:8008 timeout remains a separate issue.

The second supplied capture timestamp, 1793960576.268672, is **88.023 ms earlier**
than 1793960576.356695. Relative to it, user-space receipt is 6.361886 seconds later;
relative to the packet-list time it remains 6.273863 seconds. Neither difference
is explained by ordinary datetime rounding. Inspect the original capture versus
Wireshark's currently shifted/displayed timestamps and all candidate frames.
No packet-to-sample identity or queue delay is claimed as proven.

### Implemented diagnostic correction

`follow.rs` now persists a rolling maximum of **16 kernel-confirmed repair sends**
in `checkpoint.repair_request_history`, using the existing checkpoint publication
after send. It retains session, selected identity, endpoint, local socket, nonce,
slot, kind, byte count and user-space post-send timestamp. It persists across
sessions and removes only the oldest entry in this newly introduced bounded
diagnostic history. Existing checkpoint claims and counters are preserved.
A crash before checkpoint publication can still leave a send absent from history.
The records do not prove physical transmission, ownership or remote receipt.

This fills the confirmed loss of prior peer-selection evidence. It does not drain
queues, accept late packets, answer another source, alter retries or change any
network authorization. A regression was added for excluding unconfirmed sends,
preserving prior-peer metadata, bounding history and retaining cumulative counters.
Tests/builds were not executed; `git diff --check` passed. Operator verification:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
```

### Existing-evidence commands

On Monolith, inspect the public metadata only:

```bash
sudo jq '{repair_session_id,bootstrap_nodes,active_repair_peer,last_repair,
  repair_request_history}' /var/lib/freya-node/follow/checkpoint.json
sudo jq '{planning_run_id,bootstrap_nodes}' /var/lib/freya-node/follow/window.json
```

Old deployments cannot retroactively supply the new history. If an existing
operator-created diagnostic snapshot contains this identity/endpoint, compare
its advertisement's identity and repair socket, not merely its gossip address.
Never search or expose `identity.json` or wallet files.

Using the existing `cap` variable, extract **both directions** for the earlier-peer
hypothesis, including outbound requests before the ping. Include the currently
selected peer as a separate flow:

```bash
tshark -r "$cap" --disable-protocol wg \
  -Y 'frame.time_epoch >= 1793960560 && frame.time_epoch <= 1793960593 && udp && ((ip.addr == 185.8.106.229 && udp.port == 8009) || (ip.addr == 64.130.50.70 && udp.port == 8008) || udp.port == 49458)' \
  -T fields -e frame.number -e frame.time_epoch -e frame.interface_id \
  -e ip.src -e ip.dst -e udp.srcport -e udp.dstport -e udp.length
```

Compare original-file output with the Wireshark packet-list and expanded frame
views for frame 2941762. Record Time Shift settings, selected frame/interface,
clock source and whether views were taken from the same saved capture. Do not
silently choose either timestamp. Match the exact verified payload across frames;
source/length alone cannot distinguish repeated pings. The signed ping has no
request nonce, so earlier-request correlation relies on selected identity,
endpoint/socket, request chronology and payload evidence rather than its trailer.

If capture/history does not contain a prior request and matching identity, leave
that relationship unresolved. Do not initiate a new session automatically. After
the existing reviewed refresh/activation workflow, one explicitly authorized
bounded diagnostic may use the new history and the previous two-interface plan.
All lease, bandwidth, restart, endpoint, signature and nonce restrictions remain.
