# November 7 repair handshake correction

Reviewed checkout: `7c4efcf`. Evidence is the operator's handoff; the newest
Monolith checkpoint, frame 3468578 payload and capture are unavailable here.
No claim is made about that packet's signer or the November 7 session's results.

## Confirmed source findings versus hypotheses

Pinned Agave `ServeRepair::handle_requests` discards the initiating request when
its sender/address ping-cache check fails. It may send a signed repair ping.
An authenticated pong warms the cache but does not replay the discarded request.

Freya already verifies a selected endpoint's ping and signs a pong, but its next
attempt previously advanced round-robin selection. With several peers it could
spend the remaining bounded session probing other cold peer caches instead of
following up the completed handshake. This scheduling gap is confirmed statically;
it is **not established as the cause of the supplied runtime failures**, which
have not yet shown an authenticated ping/pong in application diagnostics.

The socket is reused across attempts and left unread during bounded backoff.
Delayed packets can be queued and read during a later peer's wait. Rejection of
an old peer while expecting a different endpoint is intentional, not grounds to
relax source or nonce validation. No receive timeout or backoff was extended.

Frame 3468578's 132-byte tag-zero payload is consistent with repair ping framing.
The full bytes must still verify under identity
`B27QcNfBDCRez4DUvoahTeR9yCuz4vSj6zQaVQeJW6pZ` for Request B, at
202.8.11.8:8009, socket 60404. Endpoint equality alone is insufficient.
The earlier 185.8.106.229 packet's verified identity does not authenticate this
packet. A repair ping has no request nonce trailer.

## Implementation

- `native/freya_node/src/follow.rs`: after a successful authenticated pong and a
  completed wait without a shred, prioritize **one existing next attempt** for
  that same identity/socket pair, if it remains in the current eligible peer list.
  The follow-up consumes the normal attempt count, pacing and namespace quota.
  It cannot chain another priority follow-up; ordinary rotation resumes. Preference
  is memory-only and does not authorize traffic across process restarts.
- Record `handshake_followup` in current-attempt metadata and bounded send history;
  also record receive-window bounds, last expected-source receive time and pong
  send time. These are user-space timestamps, not kernel arrival or wire proof.
- `native/freya_node/src/repair_bridge.rs`: expose checked ping/pong results using
  the same pinned decoder, signature verification and signer equality. Bounded
  categories distinguish framing/decode, bad signature, signer mismatch and pong
  encoding failure. The compatibility wrapper remains. Only expected-source,
  132-byte tag-zero candidates enter this path; normal shred validation remains.

No unsolicited or historical endpoint can trigger a pong or prioritized request.
No additional immediate send, timer extension, lease renewal, autonomous restart,
slot skip, source-pin reset or trading admission was introduced. All existing
attempt, memory/disk, bandwidth and checkpoint restrictions remain.

## Validation

Eight exact production-helper regressions passed in an isolated offline harness,
including the new same-identity/endpoint follow-up, stale endpoint/identity
fallback and consumed-preference checks. Existing malformed-packet, endpoint,
nonce, signature and deadline fixtures remain; pinned bridge tests were extended
for corrupt signatures, wrong identities and short ping frames.

The full cryptographic fixtures and engine compilation are **unverified** here.
`nix build --offline --no-write-lock-file .#native-peer .#native-node` was attempted
but the Nix daemon returned `Connection refused`. `git diff --check` passed.
No services, live queries or mainnet sessions were started.

On Monolith, with its daemon and dependencies available:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
```

The peer derivation runs the complete Rust binary fixtures. Both the follow engine
and appended repair bridge require a new pinned peer package. Regenerate the
immutable Nix add-on through the existing refresh/diff/backup/build/dry-activate/
test procedure in [native_network_investigation.md](native_network_investigation.md),
verify node and peer references, and preserve its existing stop/restore/test
rollback procedure. Do not merely rebuild while retaining an older deployed peer.

## Existing evidence needed before another session

Read the newest checkpoint, not the supplied older counters:

```bash
sudo jq '{state,updated_at,last_error_category,repair_session_id,window_start,
  next_slot,head_age_seconds,repair_requests,repair_responses,validated_shreds,
  last_repair,repair_request_history}' /var/lib/freya-node/follow/checkpoint.json
cap=/absolute/path/to/existing-private-capture.pcapng
tshark -r "$cap" --disable-protocol wg -Y 'frame.number == 3468578' \
  -T fields -e frame.time_epoch -e frame.interface_id -e ip.src -e ip.dst \
  -e udp.srcport -e udp.dstport -e udp.length -e udp.payload
```

Keep the output private. Use the existing offline signature-verification command
in [native_udp_wireguard_review_20261106.md](native_udp_wireguard_review_20261106.md)
on the exact payload. Compare its signer with Request B's identity above. Match
history session, endpoint, local port and timestamps; use request nonce 1437183817
for shred replies, not the ping. Locate the outgoing pong and subsequent request
as separate packets. If captures lack namespace-side evidence, uplink arrival
alone does not establish socket delivery. New receive timestamps will help classify
late delivery, but cannot retroactively reconstruct old waits.

## Historical-gap recovery and next bounded diagnostic

Slot 465539000 remains unresolved. A stale cached window is not evidence that a
selected repair peer still retains its data, even when RPC retention previously
covered it. Treat availability separately from handshake/UDP failures.

First retain the window, source pin, cursor, raw evidence and selected peer/history.
Seek operator-reviewed evidence of an archival serving peer's retention for the
exact slot and compatible network/version. Do not change `initial_slot`, delete
the window, fabricate skip provenance or clear the cursor. Current follow does
not expose a repair-peer pin; changing a gossip bootstrap alone does not guarantee
selection of an archival repair server. If an archival source is necessary, review
that targeted recovery configuration/implementation separately before activation;
no automatic fallback or unreviewed data import is authorized by this correction.

After offline build and deployment review, obtain explicit authorization for one
existing capped session. Preserve before/after metadata and passive uplink/veth
captures using the existing bounded procedure. Inspect the progression:
expected-source packet → ping signature/identity accepted → pong kernel acceptance
→ paced `handshake_followup=true` request → validated shred and durable raw file.
Record rejection category or receive/deadline outcome if progression stops.
The 55-second lease and 40-second follow timer remain unchanged; no automatic retry
of the diagnostic. Only actual current-session authenticated shred evidence meets
the next milestone. Reconstruction and paper-feed recovery remain gated afterward.
