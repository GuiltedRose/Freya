# November 8: smallest defensible acquisition step

Reviewed checkout: `0738c52`, session report `1518750-1794189311069321131`.
The supplied phase snapshot is 171,139 RX + 206,594 TX = 377,733 namespace bytes
at first eligible discovery. The 320 request payload bytes and 132 repair-socket
receive bytes cover a different, longer interval. No protocol share is computed
by subtracting those figures. No capture, final grant row or full latest history
is available here to complete attribution.

## Confirmed versus unresolved

Discovery succeeded. Two requests reached kernel acceptance; no ping was
authenticated and no shred validated. `handshake_followup=false` is expected until
an expected-endpoint, expected-identity ping verifies and a pong is sent. It is
not evidence that the new follow-up selection is broken.

The 86.105.224.20:8008 packet remains unknown without its full payload. Its size
is consistent with a repair ping; neither size nor a nonce-trailer mismatch
establishes its identity. A repair ping signs its token, not a request nonce.
A delayed first-peer reply read while expecting the second is plausible, not
proven. Strict endpoint rejection remains unchanged.

The next change is **offline evidence correlation**, not another receive-window,
gossip, quota or protocol change. `inspect_repair_ping.py` validates the pinned
132-byte tag-zero layout and Ed25519 token signature using solders, then searches
the existing bounded request history for a prior kernel-confirmed send matching
signer, endpoint, session and local socket. Capture destination port must match
the recorded repair socket; account for NAT before choosing a frame. This is
correlation evidence, not proof of address ownership or authorization to respond.
No packet is sent, admitted, copied into acquisition state or treated as a shred.

The tool explicitly reports that matching checkpoint metadata alone cannot prove
the captured packet and application's unexpected sample are byte-identical.
It reports capture/application delay without attributing it to socket queuing.
Raw payload/key material is not printed; the signer is public. Input reads are
bounded and output never modifies the checkpoint, ledger or provider state.

## Existing evidence and operator commands

Before another session, obtain the matching controller final grant row and the
complete checkpoint projection, including `repair_request_history` and all
`last_repair` fields. Use the existing grant/interface attribution commands in
[native_network_grant_20261107.md](native_network_grant_20261107.md). Separate
known gossip/repair socket flows, RPC/DNS/ICMP and unknown traffic on one capture
interface before comparing IP-byte totals to interface accounting.

For the unexpected packet, identify its frame in the existing capture by
86.105.224.20:8008 → local port 34886 and the attempt interval. Check all matching
frames and earlier requests, not only the current 70.40.185.139 target. Extract one
exact `udp.payload` privately with WireGuard dissection disabled as described in
[native_udp_wireguard_review_20261106.md](native_udp_wireguard_review_20261106.md).
Save a private checkpoint copy containing only the projection below and one hex
payload per file; do not copy/read `identity.json`:

```bash
sudo jq '{repair_session_id,last_repair,repair_request_history}' \
  /var/lib/freya-node/follow/checkpoint.json
```

In the existing `native-mainnet` shell (Python and solders prerequisites):

```bash
python inspect_repair_ping.py \
  --checkpoint /absolute/path/to/private-checkpoint.json \
  --payload-hex /absolute/path/to/private-payload.hex \
  --source 86.105.224.20:8008 \
  --destination CAPTURED_DESTINATION_IPV4:34886 \
  --capture-epoch CAPTURED_FRAME_EPOCH
python -m unittest tests.test_inspect_repair_ping
```

Replace both capture placeholders with actual frame metadata. The tool has no
networking dependency or runtime integration; no native deployment refresh is
needed to use it. Regression fixtures cover a signed ping with prior-send metadata,
wrong identity/endpoint/session/socket/time, wrong captured destination port,
forged signature, gossip framing and absence of admission. **All four inspector
tests passed** using cached Python 3.13/solders dependencies. On Arch, the launcher
and full pinned native-peer executable built successfully; all **39 native-peer
tests passed**, including cryptographic ping/pong, response validation, shutdown,
checkpoint and reconstruction fixtures. The pinned source was overlaid with the
current repository engine/bridge files in a temporary workspace. `git diff --check`
passed. No NixOS-specific tests or live network sessions were run. These results
do not establish successful acquisition from the real peers in the capture.

A correlated signed first-peer ping would support delayed handshake processing;
a signed but unmatched ping leaves peer/request relationship unresolved. Neither
result permits answering unexpected endpoints. If the selected peer's response
is in capture, inspect its timestamp against receive bounds, signer/rejection
category, pong, and the next existing paced follow-up request.

## Historical gap and next diagnostic gate

Preserve slot 465539000, its source identity and all incomplete evidence. Gossip
eligibility does not prove historical retention. Seek operator-reviewed availability
for that exact slot at a candidate/archive before changing acquisition sources.
Missing history must not cause a cursor jump or fabricated skip. Do not conflate
a pruned slot with a failed ping handshake.

Only if existing captures/history cannot resolve the relationship should the
operator explicitly authorize one more existing capped session. Retain before/after
history, a finite private uplink/veth capture, controller grant/stop records and
actual socket ownership. Measure the selected request through both capture points
and look for authenticated ping → pong → same-peer follow-up → valid shred.
Do not add sessions, retries, network privileges, or a finite-discovery transition
for this investigation. All existing 1 MB, 55-second, restart, signature, endpoint
and paper-only constraints remain.

After a real source/nonce/signature-validated shred is durable, validate reconstruction
and the existing offline indexer/decoder/candle path before comparing an asset with
an external reference at matching timestamps. None of those later milestones has
been established by the current report or this offline utility.
