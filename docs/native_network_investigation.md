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
  --features rpc-engine tls_connect_timeout_is_distinct_from_request_timeout
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
Do not change runtime networking until the failing layer is established. The
follow-up below adds diagnostics and offline coverage. Native readiness still
requires actual finalized-slot acquisition and reconstruction.

## Follow-up: corrected controller, remaining planning failure

### Latest findings: pacing versus transport, and lease-controlled shutdown

The operator reports that `ccb700c` passed 17 Python checks and the native builds,
was deployed through a refreshed add-on, and then completed six RPC stages in a
new attempt. `getSlotLeaders` took 30.942 seconds; the 55-second lease ended while
`getClusterNodes` was active. This is progress through planning, not evidence of
UDP repair. Historical successful planning before `ccb700c` must not be confused
with that newer attempt.

Code review found a concrete source of approximately 30-second waits **outside**
the HTTP timeout: `IndexRpc._reserve()` catches `ProviderBackoff`, records a wait,
sleeps, and retries the reservation. Remote providers default to a shared
10-request burst and at least 30-second cooldown. Prior sessions and other
consumers share this count, so the wait can land on different planning methods.
That mechanism fits the observations but is not confirmed as their cause until
the matching timing/audit evidence is inspected. No cooldown, pacing or retry
policy is changed or reset by this correction.

The existing progress snapshot now carries `rpc_timing` for the active call and
each completed RPC stage:

* `phase`: reservation, provider backoff, HTTP request/in progress/finished, or
  response validation. An interrupted reservation remains visible without an
  invented transport timeout.
* `reservation_elapsed_ms`, reservation attempt count, backoff sleep seconds and
  next requested sleep. Reservation time includes SQLite contention, ordinary
  request spacing and cooldown waits; backoff sleep is reported separately.
* `http_bridge_elapsed_ms`, one HTTP attempt and zero backend fallbacks.
* Native `request_headers_ms` (connect/TLS/request through response headers),
  `response_body_ms`, total transport time, bytes and a fixed transport phase.
  Reqwest does not expose separate successful DNS/TCP/TLS timings or connection
  reuse here; do not infer those from the combined header interval.

During planner HTTP calls only, one local sampler updates that same atomic,
owner-only snapshot at most once a second. It performs no network requests.
Native byte/phase snapshots show the last observed transfer progress; their
elapsed values can stop advancing during a stall. The Python bridge elapsed
time and stage elapsed time continue advancing. Final native timings cover
completion or an observable transport failure. Exception category/phase fields
remain in the matching `planning-error.json`. No raw payload or endpoint enters
the snapshots. Publication uses a lock to serialize sampler and planner writes.

Shutdown previously checked child exit before stop/lease state, so a cgroup
SIGTERM could be mislabeled as a Python failure. A signalled child now records
an observed lease invalidation, explicit stop, or `planner_external_signal` if
neither is established. The supervisor retains the expiry from the validated
lease and reports `network_lease_expired` when the observed termination reaches
that expiry. It preserves the child signal. An independent nonzero Python exit
still reports `planner_process_failure`; a signal alone does not prove why
systemd stopped the service. SIGKILL of the supervisor itself still requires its
systemd journal/result because no process can publish after being killed.

After validated planning/bootstrap metadata, the follower validates and persists
the structured source pin and numeric window cursor before starting gossip,
publishing `state=discovering`. Resuming the same window preserves its cursor
and attempts. Errors now retain bounded stage categories in the checkpoint
instead of collapsing every post-planning error into one generic category.
`native_ready` remains false until its existing evidence requirements are met.

Partial RPC results remain diagnostic only. Existing source-matched, time-bounded
window cache rules are unchanged; no stale head or new partial cluster cache is
introduced. Interrupted planning never publishes an incomplete trusted window.

All changes and added regressions are **not executed**: no tests, builds, Nix
evaluation, activation or network session were run by the assistant. Existing
operator results apply to `ccb700c`, not this revision. In the native-mainnet
shell, the operator's focused validation commands are:

```bash
python -m unittest tests.test_native_window_planning tests.test_rpc_diagnostics tests.test_rpc_burst_pacing
cargo test --manifest-path native/solana_binary/Cargo.toml --features rpc-engine rpc::tests::
nix build --no-write-lock-file .#native-peer .#native-node
```

Then follow the complete refresh/diff/backup/install/build/dry-activate/test and
rollback procedure above. Both the Python worker/native HTTP extension and peer
engine changed; verify refreshed package paths. Keep the existing kernel MTU
regression and run it before a further WAN diagnostic as documented above.
Only after offline checks pass and explicit authorization, start the one bounded
diagnostic below. Inspect matching run IDs and compare reservation versus HTTP
timings before another networking change. If shutdown occurs, inspect both the
supervisor record and systemd result. If planning succeeds, inspect checkpoint
`rpc_source_identity`, `window_start`, `next_slot`, `state` and repair counters.
No automatic retry, renewal, trading activation or increased limits is added.

### Structured source identity correction

The next operator report showed successful planning followed by an immediate
follower failure. `ProviderBudget.identity()` returns an object containing
`provider_id` and `endpoint_id` (lowercase, 64-character HMAC-SHA256 hex digests).
Both follower checks instead used `as_str()`. The repair-loop conversion therefore
failed on valid planner output; the earlier comparison also failed to enforce a
previous structured source pin. This is a confirmed code mismatch, consistent
with the reported post-planning exit; the corrected runtime is not yet validated.

The follower now validates that exact object shape, compares both fields against
any persisted structured pin, and persists the object intact. Missing/null prior
identity permits first use. Malformed objects and legacy string pins fail closed:
there is no automatic reset or unverifiable migration of a previous source pin.
If such a pin exists, preserve it and obtain an explicit reviewed migration
before proceeding. Checkpoints, slots, identity keys and evidence are retained.

Added Rust coverage exercises structured first use, persisted JSON round-trip,
changes to either digest, malformed metadata and legacy pins. The Python fixture
now uses the actual object shape and asserts it appears intact in `window.json`.
No tests or builds were executed. The operator validation commands below cover
these regressions; do not authorize another network attempt before they pass.
Deploy both planner and peer engine using the existing refresh, review, backup
and temporary activation procedure above. Rollback uses that saved add-on and
does not restore or erase runtime data.

### Confirmed planning deadline: current correction

The operator then identified systemd's `RuntimeMaxSec=52` as the next cutoff.
The peer-discovery unit now sets `RuntimeMaxSec=infinity`; its required network
controller and `BindsTo` relationship still stop it when the existing 55-second
lease ends. This removes the competing service timer, not the network session
limit. Refresh and activate the generated add-on using the commands above, then
verify before authorizing another diagnostic:

```bash
sudo systemctl show freya-peer-discovery.service -p RuntimeMaxUSec -p BindsTo
```

Expect `RuntimeMaxUSec=infinity` and a binding to
`solana-budget-network.service`. No tests or builds were run for this change.

The operator subsequently confirmed the 40-second planning deadline as the
failure cause. Planning now runs without that local deadline; the supervisor
still checks explicit stop and the existing network lease every 100 ms, including
caught-up waits. No network-session renewal is introduced. The configured
40-second native repair timer starts after initial planning/bootstrap metadata
is ready. It does not extend the independent 55-second lease or byte allowance;
if planning exhausts the lease, the session still stops. The 15/30-second HTTP
timeouts, firewall, shapers, ledgers and persistent evidence remain unchanged.

The timing discussion below describes the investigation before this correction.
The existing supervisor supports deadline classification for regression coverage,
but production planning no longer supplies a local deadline. Added offline
regressions cover completion without a local deadline and continued lease
enforcement. No tests or builds were executed for this correction.

For an operator-controlled retry, use the refresh/review/backup/build/temporary
activation procedure above, then the single manual diagnostic command below.
The changed component is `freya-peer`; a bundle-only rebuild does not activate
it. Roll back with the saved add-on as documented above. Inspect current-run
supervisor/progress records after the attempt, even if the service fails. Do not
automatically retry or renew the lease.

The operator subsequently confirmed the old deployed 1500-byte controller and
replaced it with the 2048-byte/65536-byte implementation. Reported corrected
sessions lasted 40.095 and 30.477 seconds, with substantially more inbound data.
The old `planning-error.json` predates these sessions. Neither the 40-second
deadline nor a 30-second request timeout is an established cause of these runs.
The latest Monolith state is not available in this checkout.

The implementation now publishes three private, atomic, bounded snapshots:

* `planning-supervisor.json`: authoritative attempt ID, termination reason,
  exit code/signal, elapsed time, matching last stage and validation outcome.
* `planning-progress.json`: the same attempt ID, stage/method, start times,
  elapsed times and completed RPC/publication stages. Local metadata validation
  is identified separately. RPC completion means the call returned, not that
  its metadata or an entire window passed validation.
* `planning-error.json`: current attempt's allowlisted category, transport
  phase, numeric status/errno/bytes and transport elapsed time when available.
  A fresh attempt replaces stale classification with `in_progress`; success
  sets `none`. Historical node/checkpoint/evidence data is not reset.

Only compare records with equal `run_id`. Each caught-up retry has a fresh ID.
The supervisor writes first, even before launching Python: if the IDs differ,
Python's snapshot is stale. A `running` snapshot plus `planning_deadline`,
`stop_requested` or `network_lease_invalid` means the supervisor interrupted it;
Python cannot catch SIGKILL. `planner_process_failure` plus a matching error
identifies an independent Python failure. Inspect its category and phase to
distinguish TLS/connect, request/body timeout, throttling and authorization.
The checkpoint's legacy generic category remains for compatibility; use the
supervisor record for precise planning outcomes. `planning_started` without a
terminal record requires checking systemd's result/signals (for example an OOM
or external SIGKILL of the supervisor itself).

`window.json` now includes `planning_run_id`; supervisor success requires that
ID and the existing provenance/genesis/start checks. An older retained window
cannot establish success for a new planner process. No incomplete window is
published. Diagnostic write failures fail closed; disk bounds and all network
limits remain unchanged. Deploy Python and Rust together via the refresh above.

### Timing review

The uncached planner makes seven sequential calls: genesis, head, history floor,
leaders, manifest, history recheck and cluster nodes. The existing valid cache
can omit cluster nodes. `RustIndexRpc.call` reserves shared pacing before sending
each request and reuses its transport. It performs no HTTP fallback/retry, but
its inherited reservation loop can wait and retry provider-budget reservations.
`ProviderBudget.reserve` can sleep for a reservation or reject an active cooldown;
neither state is cleared. The Rust transport collects the complete response
body under its existing 30-second request timeout, with a 15-second connection
timeout. The supervisor's 40-second duration includes all calls, pacing,
validation, publication and subsequent native work; the lease remains 55 seconds.

At the configured 16 KB/s download rate, 400 KB takes roughly 25 seconds before
handshake, pacing and other work. Systemd byte totals include traffic beyond
unique response bytes, so this is a capacity illustration, not measured request
size or proof the workflow cannot finish. Current stage wall times and transport
`elapsed_ms` on failures will bound where time is spent; connection reuse itself
is not directly observable through reqwest. No timeout or budget change follows
from the existing evidence.

### Operator validation and one diagnostic

No new tests, builds, Nix evaluation or mainnet requests were executed by the
assistant. Passed/failed/skipped counts for the new changes are unknown; all
new regressions are **not executed**, per operator instruction. The previously
reported Rust TLS/RPC and kernel results apply to the earlier implementation.

In `nix develop .#native-mainnet`, run:

```bash
python -m unittest tests.test_native_window_planning tests.test_rpc_diagnostics
cargo test --manifest-path native/solana_binary/Cargo.toml --features rpc-engine rpc::tests::
nix build --no-write-lock-file .#native-peer .#native-node
```

`native-peer` applies the local follow supervisor to the pinned Agave workspace
and runs its deterministic fixtures, including deadline, stop, invalid lease,
child exit, success and redaction tests. Running Cargo against
`native/freya_node/Cargo.toml` would only test the launcher, not this supervisor.
Retain and run the full-MTU kernel command above. These checks require the
existing dependencies/toolchain and, for the kernel test, Monolith privileges.

After these pass, use the complete refresh/review/build/dry-activate/test and
rollback commands above. Verify both `freya-node` and `freya-peer` deployment
references: follow code resides in the peer engine. Then, only if explicitly
authorizing one existing capped session, run once:

```bash
sudo systemctl start freya-peer-discovery.service
sudo systemctl show freya-peer-discovery.service \
  -p InvocationID -p Result -p ExecMainCode -p ExecMainStatus
sudo journalctl -u freya-peer-discovery.service -u solana-budget-network.service \
  -b -n 100 --no-pager
sudo cat /var/lib/freya-node/follow/planning-supervisor.json
sudo cat /var/lib/freya-node/follow/planning-progress.json
sudo cat /var/lib/freya-node/follow/planning-error.json
```

Inspect these even if `systemctl start` returns failure. Do not rerun automatically.
Match IDs and timestamps, and retain the results privately. A successful planning
record establishes only a validated planning manifest; actual finalized-slot
acquisition and reconstruction are still required before native readiness.


### Provider cooldown and resumable planning (2026-11-04)

Planning now requests cluster metadata immediately after genesis validation,
so the large response is not deliberately placed after six burst reservations.
It atomically retains validated public bootstrap claims and the current window's
finalized manifest/schedule in owner-only `planning-cache.json`. Claims expire
at 300 seconds, are bound to both provider and endpoint digests, and never renew
their timestamp on reuse. Genesis authorization, finalized head, and both history
floor checks still run on every attempt. Cache contents pass structural checks
before use; partial HTTP bodies are never cached. No session is automatically
renewed, and provider reservations/accounting remain in the existing RPC path.

The supervisor passes the existing lease expiry to the planner. When a reported
cooldown cannot fit in the remaining lease, the planner exits 76 before sleeping
or issuing that request; the final supervisor category is
`provider_cooldown_exceeds_lease`. It is a deferred attempt, not successful
planning. A hard lease interruption remains `network_lease_expired`. The final
supervisor also copies bounded RPC phase/wait/body metrics from the matching run,
so expiry during cooldown can be distinguished from expiry during body transfer.
A short authorized lease still cannot guarantee completion of an arbitrarily
slow provider response. Wait for cooldown outside the next authorized session;
never clear the shared ledger or automatically grant another session.

Operator prerequisites: existing reviewed Monolith configuration, dependencies,
and the `native-mainnet` development shell. Run the following focused validation:

```bash
nix develop .#native-mainnet
python -m unittest tests.test_native_window_planning
nix build --no-write-lock-file .#native-peer .#native-node
```

The peer build includes the supervisor regression fixtures. Then follow the
refresh, diff review, backup, build, dry-activate, activation and rollback commands
at the start of this document. Run only the single explicitly authorized session
shown above; collect all three diagnostics even on failure. Acceptance requires
matching run IDs with `planner_success` and `window_validated: true`, followed by
actual native peer discovery and repair evidence in the existing follow status
and journal. `provider_cooldown_exceeds_lease` or `network_lease_expired` is not
acceptance. No operator diagnostic was supplied with this change, so its actual
shutdown classification and end-to-end acceptance remain unverified.

Rollback uses the saved Nix add-on and the existing stop/restore/test commands.
Retain the new cache alongside all existing persistent data; older code ignores
it. No wallet, transaction, training, checkpoint or accounting reset is needed.
The assistant performed static review and `git diff --check` only; these tests,
builds, activation and network operations remain for the operator.
