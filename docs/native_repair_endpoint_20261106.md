# November 6 repair endpoint review

The supplied operator evidence establishes outbound gossip associated with Freya,
four advertised repair candidates, and application receipt of one unexpected UDP
datagram on the repair socket. `peer_connected` counts candidates, not established
UDP connections. Counters are cumulative; compare session IDs and before/after
snapshots. No validated repair response or authenticated shred is yet evidenced.

For the last attempt use 109.122.207.111:8008 and local repair port 53253, not
previous targets or gossip port 8000. `response_timeout` identifies a completed
response wait rather than the distinct stop/deadline/error outcomes. It does not
prove that no packet arrived: the unexpected datagram was received and rejected.
The report omits `expected_datagrams`, `rejected_datagrams`, `last_rejection`,
`authenticated_pings`, `pongs_sent`, timestamps and nonce, so it cannot distinguish
silence from rejected expected-source packets or a ping-only exchange.

## Static findings and smallest change

`follow.rs` chooses recent same-version gossip contacts, filters their advertised
UDP repair endpoints, and rotates among candidates under existing attempt bounds.
Requests bind sender, recipient, timestamp and nonce using pinned Agave signing.
Acceptance requires the selected exact source socket, nonce trailer, appropriate
index and sanitized data shred with matching slot/version and leader signature.
The source check remains correct for the authorized outstanding request. It must
not be relaxed merely because another source sent a datagram.

The receive loop previously discarded the unexpected source after incrementing
its counter. Its identity cannot be recovered from that checkpoint. Possibilities
include a delayed response from a previous peer using the same local socket,
unrelated traffic, or a peer replying from a different endpoint. None is proven.
A valid signature or matching nonce alone does not authorize another endpoint.

The only implementation change is one bounded `last_repair.unexpected_sample`:
first source address/port, receive timestamp, received length and an untrusted
nonce-trailer comparison. It stores no payload and changes no admission, reply,
retry, lease or validation behavior. A matching trailer does not prove authenticity.
The sample resets with each new attempt; the existing counter still counts all
unexpected packets. An interrupted attempt can retain only its last published
snapshot. No new immediate writes or unbounded history were introduced.

Regression coverage was added for first-source retention, counting multiple
sources, short packets, and no admission of unexpected data. Tests/builds were
not executed in this investigation; `git diff --check` passed. No production
protocol defect or cause of the reported timeout has been established.

## Read existing evidence first

On Monolith, without starting another session:

```bash
sudo jq '{state,updated_at,repair_session_id,last_error_category,repair_requests,
  repair_responses,repair_timeouts,validated_shreds,last_repair}' \
  /var/lib/freya-node/follow/checkpoint.json
```

Inspect the existing namespace/veth capture for **all** datagrams delivered to
53253, including sources other than the selected peer. This is needed to identify
the unexpected sender. Restricting the filter to the expected peer would hide it.
Keep raw captures private. Replace the path with the actual existing capture:

```bash
tshark -r /absolute/path/to/private-capture.pcapng \
  -Y '(udp.dstport == 53253) || (ip.addr == 109.122.207.111 && udp.port == 8008) || icmp.type == 3' \
  -T fields -e frame.number -e frame.time_epoch -e frame.interface_name \
  -e ip.src -e ip.dst -e udp.srcport -e udp.dstport -e udp.length \
  -e icmp.type -e icmp.code
```

Filter further to the attempt's `started_at`/`finished_at` epoch interval. On the
namespace side the expected response is 109.122.207.111:8008 → local port 53253.
Uplink NAT may change the public local port: match request egress and conntrack/
veth tuples before deciding a captured packet reached this socket. Compare UDP
lengths carefully: Wireshark `udp.length` includes the eight-byte UDP header.
Use frame timestamps and the logged nonce to examine a candidate payload locally;
an unexpected packet might belong to an earlier request. Do not disclose complete
captures or interpret gossip packets as repair responses. ICMP matters only if
its quoted UDP tuple matches the request.

For lifecycle correlation, run the journal/lease-event commands in
[native_network_failure_20261105.md](native_network_failure_20261105.md) with the
latest attempt's actual host-local interval. A clean service shutdown does not
validate packet acceptance or explain every earlier request's outcome.

## Validation and any next diagnostic

Operator offline checks:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
```

The peer build includes the new regression and existing nonce/socket/signature
fixtures. Follow the existing refresh/review/backup/build/dry-activate/test and
rollback workflow in [native_network_investigation.md](native_network_investigation.md)
only when deploying reviewed changes. No state reset is needed.

Prefer the existing captures before spending another grant. If they lack the
repair flow, obtain explicit authorization for exactly one bounded session with
passive uplink and namespace-side capture. Use that session's endpoint/local port,
not these historical values. Retain before/after checkpoint snapshots and inspect
`unexpected_sample`, expected/rejected counts, ping/pong counts and outcome.
Keep `Restart=no`, the 55-second lease, bandwidth/quota and authorization intact.
Do not automate retries or start trading.

Acceptance still requires a genuine signature-validated, nonce/socket-correlated
shred durably present after shutdown, with matching session evidence. Only then
proceed to reconstruction and subsequent execution-receipt/paper-feed work.
