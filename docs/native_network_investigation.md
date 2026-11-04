# Native RPC packet-delivery investigation

November 4, 2026: the operator reports TCP establishment, a retransmitted
1448-byte server payload, and a client reset after about 15 seconds on the
physical uplink. Stream ownership and namespace delivery are not established.
The earlier planning-error timestamp must not be attributed to a later run.

`native/solana_binary/src/rpc.rs` configures a 15-second connection timeout and
a 30-second request timeout. Its existing
`tls_connect_timeout_is_distinct_from_request_timeout` regression accepts TCP
without completing TLS and expects `connect_timeout`. Thus that category does
not establish a TCP-handshake failure. If the captured stream is Freya's, the
leading hypothesis is a stalled TLS handshake; packet loss, filtering, shaping
and provider behavior remain possible causes. No further networking correction
is justified yet. Keep the DNS and full-frame shaping repairs.

## Operator checks before another session

### Refreshing a deployment that keeps using an old build

This repository's `flake.lock` pins dependencies, not Freya itself.
`prepare_mainnet_host.py` writes immutable application and module store paths
into `freya-mainnet.nix`. Rebuilding a bundle or updating dependency locks does
not replace that installed add-on. Refresh it from the intended checkout instead.

Prerequisites: sync the intended code to Monolith, preserve the existing private
configuration and ledgers, and wait until all three Freya units are inactive or
failed. Review `git status --short`; Git-backed flakes omit untracked files.
The existing `/etc/nixos/freya-diagnostic-host.nix` wrapper must already be the
reviewed diagnostic configuration. These commands build and temporarily activate
configuration; they do not authorize starting another network session.

```bash
cd ~/repos/Freya
git status --short
git rev-parse HEAD
nix develop .#native-mainnet
```

Inside that shell:

```bash
refresh_dir="state/mainnet-refresh-$(date -u +%Y%m%dT%H%M%SZ)"
python prepare_mainnet_host.py --refresh --output "$refresh_dir"
diff -u /etc/nixos/freya-mainnet.nix "$refresh_dir/freya-mainnet.nix"
```

Review the diff: new immutable store paths are expected; identity, ledger paths,
network budgets and authorization must retain their intended values. `diff`
returns 1 when files differ. Then preserve the installed add-on and install the
reviewed replacement:

```bash
sudo cp -a /etc/nixos/freya-mainnet.nix "$refresh_dir/freya-mainnet.before.nix"
sudo install -m 0644 "$refresh_dir/freya-mainnet.nix" /etc/nixos/freya-mainnet.nix
sudo nixos-rebuild build -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
sudo nixos-rebuild dry-activate -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
sudo nixos-rebuild test -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
sudo systemctl show freya-peer-discovery.service solana-budget-network.service -p ExecStart
```

Run each command only after the preceding command succeeds and its output has
been reviewed. Compare active `ExecStart` paths against the generated add-on;
the controller path is derived from the imported source module. If the relevant
source is unchanged, identical store paths are expected and correct. Do not
change dependency pins merely to force a new hash. Run the focused validations
below before manually authorizing another diagnostic.

Rollback using the same shell's `refresh_dir` (or its recorded absolute path):

```bash
sudo systemctl stop freya-peer-discovery.service freya-node.service solana-budget-network.service
sudo install -m 0644 "$refresh_dir/freya-mainnet.before.nix" /etc/nixos/freya-mainnet.nix
sudo nixos-rebuild test -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
```

Do not restart services automatically or restore/reset data and accounting.

No commands below have been executed by the assistant. Keep unit output and
captures private: configuration and captured traffic can contain credentials.
Do not upload complete environment files or packet captures publicly.

```bash
cd ~/repos/Freya
git rev-parse HEAD
sudo systemctl show freya-peer-discovery.service solana-budget-network.service \
  -p FragmentPath -p ExecStart -p Environment -p NetworkNamespacePath -p BindReadOnlyPaths
sudo systemctl cat freya-peer-discovery.service solana-budget-network.service
sudo journalctl -u freya-peer-discovery.service -u solana-budget-network.service \
  -b -n 100 --no-pager
sudo stat /var/lib/freya-node/follow/planning-error.json
```

Inspect the immutable controller directory in the active `ExecStart`. Its
`solana_network_budget.py` must contain `SHAPER_BURST_BYTES = 2048` and
`SHAPER_QUEUE_BYTES = 65536`; the controller must use the isolated resolver.
Compare the application store path with the prepared build. A local bundle
build alone does not activate either service.

If deployment is stale, use the existing preparation/temporary activation and
rollback procedure in [selective_node.md](selective_node.md#full-frame-shaping-repair).
Do not activate solely to collect evidence if the current deployment is correct.
Run the documented offline full-MTU regression before another WAN session. Also
run the existing Rust TLS regression from the native-mainnet development shell:

```bash
cargo test --manifest-path native/solana_binary/Cargo.toml \
  tls_connect_timeout_is_distinct_from_request_timeout
```

## One explicitly authorized bounded capture

Prerequisites: the above checks pass, `dumpcap` and `timeout` are installed,
the existing diagnostic policy still grants at most 1 MB for 55 seconds, and
the operator authorizes exactly one additional session. No renewal or separate
RPC probe. Capturing is passive and does not authorize the service start.

Use Linux `any` to include the dynamically created veth; preserve SLL2 interface
indices in pcapng. Confirm the capture uses Linux cooked v2 before relying on
interface attribution. If unavailable, stop and use an interface-aware capture
tool rather than treating indistinguishable packets as delivery evidence.

In terminal A, start the bounded capture before starting the service:

```bash
sudo install -d -m 0700 /var/tmp/freya-net-evidence
sudo sh -c 'umask 077; exec timeout --signal=INT 65s dumpcap \
  -i any -y LINUX_SLL2 -p -s 2048 \
  -f "tcp port 443 or port 53 or icmp" \
  -a duration:60 -a filesize:4096 \
  -w /var/tmp/freya-net-evidence/session.pcapng'
```

Use a fresh filename for subsequent evidence; do not overwrite prior captures.
The capture may include unrelated host traffic. Avoid unrelated activity during
the session. The 4 MB capture cap bounds disk use, not network authorization.

In terminal B, start the single diagnostic asynchronously and immediately gather
active-state evidence while its namespace exists:

```bash
date -u --iso-8601=seconds
sudo systemctl start --no-block freya-peer-discovery.service
```

Once `solana-host` appears, run these read-only commands promptly; repeat socket
inspection during the handshake if necessary, without restarting the service:

```bash
sudo ip -o link show
sudo ip route get 169.254.203.2
sudo ip netns exec solana-budget ip route
sudo ip netns exec solana-budget ss -ntp
sudo tc -s qdisc show dev solana-host
sudo ip netns exec solana-budget tc -s qdisc show dev eth0
sudo nft -a list ruleset
sudo ip netns exec solana-budget nft -a list ruleset
```

Record the socket's process, local port and destination against the capture.
The veth ifindex identifies its SLL2 records. NAT changes the client address and
may change its port; correlate TCP sequence numbers and timestamps across both
interfaces. Record qdisc/counter snapshots again before session teardown if
possible. An absent veth after teardown is not evidence of active misrouting.
After completion, save the fresh journal and planning error using the first
section's commands, matching timestamps to this run.

## Reading the evidence

* Uplink receives server sequence ranges absent at the host veth: investigate
  host forwarding, conntrack/NAT and download qdisc drops. Counter deltas and
  hook ordering are needed to identify the responsible rule or queue.
* The same ranges appear at the host veth: capture inside the namespace in a
  separately authorized follow-up before claiming application delivery. Check
  namespace firewall counters and ACK progression. Host veth egress alone does
  not prove namespace TCP acceptance.
* Namespace ACKs progress but TLS stalls: inspect TLS handshake/alerts and
  retransmissions; TCP ACKs alone do not establish TLS completion.
* No corresponding server traffic reaches the uplink: investigate upstream or
  provider behavior after confirming the Freya socket and request timing.

Offloads can change captured segment sizes and checksum appearance. Do not
disable offloads, change MTU, increase timeouts, or alter limits without evidence.
An encrypted Application Data record does not prove an RPC response; TLS 1.3
also encrypts handshake records.

Capture requires no configuration rollback. Let the bounded session expire;
if intervention is needed, stop `freya-peer-discovery.service` and
`solana-budget-network.service`. Preserve all state, accounting and cooldowns.
There is no new runtime change to deploy or regression to add until the failing
layer is established. Native readiness still requires actual finalized-slot
acquisition and reconstruction.
