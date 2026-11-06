# November 5 repair interruption investigation

## Conclusion

The reported attempt reached kernel acceptance of a 160-byte UDP request to
198.13.139.39:30008 from namespace socket 0.0.0.0:44733. No accepted response or
validated shred is evidenced. `request_interrupted/native_repair_io` with process
exit 1 establishes a failed operation, not its cause. Host packet arrival is not
proof of socket delivery. No matching capture frames, precise timestamps or
controller journal for this latest failure were supplied.

**Premature shutdown is not confirmed.** The code deliberately checks authorization
before receiving even queued packets. Lease revocation/expiry can therefore stop
processing, but bypassing that check would violate the existing authorization.
The 55-second controller lease begins before namespace setup/READY. The independent
40-second follow timer begins after initial planning/bootstrap checks and before
socket/gossip setup. Each response timer begins after send/publication and is
capped at two seconds and the follow deadline. None of these observations justifies
extending a timer. Only journal/flow correlation can distinguish intended expiry
from another failure in the reported session.

`Requires/BindsTo/After` propagates controller disappearance to the peer. Stopping
the peer alone does not establish why the controller stopped. `Restart=no` remains
intentional. A controller exception, byte exhaustion, signal, lease/cycle expiry,
or configuration stop still requires evidence from that controller invocation.

## Source findings and corrections

- `native/freya_node/src/follow.rs`: `lease`, counter reads, UDP send/receive and
  persistence errors previously converged on `native_repair_io`. Failure handling
  also set the stop flag to terminate gossip, so inspecting that flag afterward
  could mistake internal cleanup for an external stop. Classification now captures
  the flag before cleanup and records operation, bounded error kind/errno, saved
  lease expiry, deadline observation and finish timestamp. A lease-check failure
  after the saved expiry is `network_lease_expired`; an earlier failure is
  `network_authorization_invalid`, which does not itself prove controller death.
  Socket failures and counter failures remain distinct. Existing accepted outcomes
  and prior-session records are retained.
- Failure evidence is published before potentially slow gossip joining. A process
  killed during join may leave `state=stopping`; the journal must still establish
  final exit. Neither that checkpoint nor durable raw data establishes exit success.
- `response` now reports bounded rejection categories: endpoint, framing, nonce,
  indexed-request mismatch or shred validation. The existing `checked` call still
  sanitizes and verifies slot, version, index bounds, data type and leader signature.
  `shred_validation_failed` groups those checks; it does not isolate one internally.
- `solana_network_lifecycle.py`: events now include expiry, READY timestamp,
  stop reason and received signal. Reasons distinguish expiry, byte-grant exhaustion,
  stop request and controller error. They do not identify who sent a signal.
  A confirmed cleanup defect was fixed: audit/close-boundary failure could previously
  skip final sampling and cleanup. Nested finally blocks now attempt both, keeping
  accounting failures fail-closed rather than swallowing them.

No protocol, peer-response guarantee, pacing, retries, lease duration, restart
policy, provider accounting, firewall, identity, wallet or trading rule was changed.
One authenticated shred is still the outstanding milestone.

## Transport checkpoints

The full path remains: recent same-version gossip contact → approved IPv4 repair
endpoint → ephemeral UDP socket → signed pinned-Agave request → namespace routing,
quota/shaping and host NAT → remote server → reverse flow → exact source socket
check → authenticated ping/pong or nonce-bound shred validation → atomic raw-file
publication/fsync → checkpoint publication. Packets can be dropped by either host,
namespace or remote policy, queued beyond the wait/lease, rejected by validation,
or never sent back. Ping verification does not make the remote replay its first
discarded request; existing bounded retries remain unchanged.

`send_confirmed` proves only kernel acceptance. Expected/unexpected packet counters
prove application receipt only when they increase in the matching repair session.
`0.0.0.0` is a wildcard bind, not the on-wire source address. Public source ports
can differ after NAT. Traffic involving 208.91.111.195:8000/8001 is not attributable
to this repair flow without further evidence. ICMP may explain a recorded socket
errno only when its quoted original UDP tuple matches; chronology alone is insufficient.

## Validation

Executed: `python -m unittest tests.test_solana_network_lifecycle` — 8 passed,
all privileged/network actions mocked. Five Rust helper regressions passed in an
isolated offline harness using exact production helpers/tests and pinned address
policy source. They cover interruption causes, redaction, evidence preservation,
endpoint policy and timeout accounting. Existing cryptographic fixtures were
extended with rejection-category assertions but could not be executed here.
`git diff --check` passed.

Attempted `nix build --offline --no-write-lock-file .#native-peer .#native-node`;
blocked by `Connection refused` at the Nix daemon socket. Full engine compilation,
crypto integration and native-node build remain unverified. No activation, mainnet
session, capture or trading action was run.

Operator verification, with the existing Nix daemon/dependencies available:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
nix develop --offline --no-write-lock-file .#native-mainnet -c \
  python -m unittest tests.test_solana_network_lifecycle
```

## Missing host evidence: exact collection

Use the failure's actual host-local time interval and confirm timezone. These
commands read existing evidence and do not start services. Keep raw output private;
share only relevant redacted records, excluding keys/environment credentials.

```bash
date --iso-8601=seconds
timedatectl show -p Timezone
sudo systemctl show freya-peer-discovery.service solana-budget-network.service \
  -p InvocationID -p ExecMainCode -p ExecMainStatus -p Result -p ExecStart \
  -p ActiveEnterTimestamp -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
  -p Restart -p Requires -p BindsTo -p RuntimeMaxUSec
# Replace both placeholders with the latest failure's actual host-local interval.
sudo journalctl -u freya-peer-discovery.service -u solana-budget-network.service \
  --since 'YYYY-MM-DD HH:MM:SS' --until 'YYYY-MM-DD HH:MM:SS' \
  -o short-precise --no-pager
sudo jq '{state,updated_at,last_error_category,repair_session_id,network_lease_expires,
  repair_requests,repair_responses,repair_timeouts,validated_shreds,
  last_reconstructed_slot,last_repair}' /var/lib/freya-node/follow/checkpoint.json
sudo cat /var/lib/freya-node/follow/planning-supervisor.json
# Confirm this path against the installed controller policy before using it.
sudo tail -n 12 /var/lib/solana-network/network-events.jsonl
```

Match invocation/session/run IDs and timestamps; current systemctl state can refer
to a later invocation. Old controller events cannot retroactively supply new fields.
The ephemeral lease file is normally deleted at cleanup. Compare installed immutable
node/peer/controller source paths with the reviewed checkout using the existing
[NixOS refresh and rollback workflow](native_network_investigation.md); do not
activate anything just to read evidence.

For an existing private capture, extract flow metadata without dumping payloads:

```bash
tshark -r /absolute/path/to/existing-private-capture.pcapng \
  -Y '(ip.addr == 198.13.139.39 && udp.port == 30008) || icmp.type == 3' \
  -T fields -e frame.time_epoch -e frame.interface_name -e ip.src -e ip.dst \
  -e udp.srcport -e udp.dstport -e udp.length -e icmp.type -e icmp.code
```

Needed frames: request egress, corresponding response ingress, and any ICMP quoted
request. Compare uplink and host-veth/namespace sides using timestamps and tuples;
uplink ingress alone cannot locate a loss past NAT/firewall. On the namespace
side the expected receive tuple is remote 198.13.139.39:30008 → local port 44733
for this old attempt. Use `last_repair` for a different attempt; do not reuse ports.
An exact response-frame payload is required for nonce/framing analysis locally,
not public disclosure of complete captures.

Only after build validation, the existing review/backup/activation workflow and
explicit authorization for one capped diagnostic may the operator start another
session. During that authorized session, passive checks are:

```bash
sudo ip netns exec solana-budget ss -u -a -n -p
sudo ip netns exec solana-budget nft list table inet solana_budget
sudo nft list table inet solana_boundary
```

Confirm the installed host table name before the last command. Collect counters
and flow evidence within the existing lease; do not extend it to collect more.
Keep `Restart=no` and no automatic retries. For a new capture, use the existing
bounded, two-interface capture procedure in the investigation runbook, updating
the remote repair-flow filter and local port from that session's diagnostics.
Rollback remains the existing stop/restore/test procedure without restoring,
resetting or deleting persistent acquisition/accounting state.
