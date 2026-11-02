# Native RPC reliability findings and repairs

## V3 implementation and current acceptance

The mainnet root cause is **not yet confirmed**. Safe diagnostics now preserve
request method, connect/send/body/cancellation phase, elapsed transport time,
received body bytes, known HTTP status, timeout classification and an available
numeric socket code. Rust cannot reliably distinguish every DNS/TLS failure from
other connect failures through its public error API; those remain connect errors,
not invented subcategories. Pooled-connection reuse is explicitly unknown (`null`).
The probe caps Python connection establishment at 15 seconds and the call at
30 seconds using an isolated worker timer, matching the native ceilings without
changing production timeouts. Python/native HTTP errors retain their actual status. Local transport backoff is
marked `local_transport`, not reported as a provider-issued 503. Explicit close
retains the existing RuntimeError contract and does not charge provider failure.
The collector's paused records include the new safe fields. No raw URL, error
chain or credential is rendered by the new transport diagnostics.

The NixOS isolated-node path now uses a 1500-byte tc burst instead of 16 KiB,
explicit 16 KiB nft policing headroom, 1500-byte veth MTUs, and disabled TSO/GSO/GRO
on **only the dedicated veth pair**. Upload rate policing moves from namespace
OUTPUT (before tc) to the host FORWARD boundary (after namespace tc); otherwise
an application write could be dropped before the shaper could queue it. Download
policing remains after the host-veth shaper. Quota and lease checks stay in the
namespace in both directions. This coordinates the existing shaper with its
policing backstop and avoids oversized offloaded packets. The configured rates,
hard aggregate byte quota, lease expiry, accounting-before-drop, namespace and
manual-start requirements are retained. Named `download_rate_drop` and
`upload_rate_drop` (host boundary) counters expose any remaining rate policing drops. Finite
queues, quota exhaustion and session expiry can still drop traffic; this is not a
claim of lossless networking or proof of Monolith's failure cause. No host/uplink
interface settings were changed by the coding agent.

`diagnose_mainnet_rpc.py` / `nix run .#rpc-diagnostics` prepares the missing mainnet
comparison. It runs Python then Rust against common finalized slots, with ten
logical requests total (genesis, finalized head, slot manifest, two single-block
requests per backend), a default 180-second process deadline and the existing shared
provider ledger. Cooldown waits do not create replacement requests; any request
interrupted by the deadline is marked unknown, never successful. The default decoder
checks authentic returned transactions through the reviewed schema configuration;
results do not admit observations to storage, qualify training labels, or verify
fresh on-chain deployment pins by themselves. Success latency includes call pacing;
failure `elapsed_ms` is transport time when available. No signer environment or
`.env` file is loaded by the subprocess. Reports are owner-only and never overwritten.
An indexer ownership lock plus the required `--workers-stopped` operator assertion
prevent accidental overlap in the normal configured deployment; the operator must
also stop independently supervised RPC consumers. A failed comparison exits nonzero.

### Operator sequence

Include the new public modules and tests in the reviewed Git revision before
Git-backed Nix builds; never stage `.env`, runtime configs, state or models. Retain
any reviewed local `flake.lock`; all commands below prohibit lock writes.

```bash
nix develop --no-write-lock-file .#native-rpc --command python validate_production.py --native --output /tmp/freya-v3-offline-new.json
nix build --no-write-lock-file .#native-rpc-app
nix build --no-write-lock-file .#checks.x86_64-linux.solana-network-lifecycle
```

The VM check is separate from the offline suite and does not certify the actual
host. Before changing NixOS network services, stop all owned paper/live/indexer/
observer/learner workloads and `solana-budget-network.service`; do not restart a
funded live service as part of this repair. For CLI supervision use Ctrl+C. For
systemd deployments, stop only the configured units listed in the deployment guide.
Back up complete runtime directories and the persistent budget ledger while stopped.

For a host configuration importing this repository's NixOS module, first ensure
its existing Freya input resolves to the reviewed repair revision; rebuilding an
unchanged older host input will not apply this repair. Replace the
example path below with the existing host flake reference; build before activation:

```bash
NIXOS_HOST_FLAKE=/absolute/path/to/host-config#monolith
sudo nixos-rebuild build --flake "$NIXOS_HOST_FLAKE"
# After reviewing the build, with owned workloads and network session stopped:
sudo nixos-rebuild switch --flake "$NIXOS_HOST_FLAKE"
```

This does not authorize a new network-budget grant. Start a reviewed bounded node
session manually only if using isolated mode; remote-provider probes do not require
one. Monitor the existing ledger and session status. Inside an authorized isolated
session these read-only commands expose the applied pacing and drop counters:

```bash
sudo tc -s qdisc show dev solana-host
sudo ip netns exec solana-budget tc -s qdisc show dev eth0
sudo ip netns exec solana-budget nft list counter inet solana_budget download_rate_drop
sudo nft list counter inet solana_boundary upload_rate_drop
```

For the mainnet comparison, use the existing reviewed config and actual index path.
If its URL is `${SOLANA_RPC_URL}`, export **only** that URL in a protected shell;
do not source a wallet `.env` or put authenticated URLs in command history. The
probe deliberately does not load `.env`. Keep all competing workers stopped:

```bash
nix run --no-write-lock-file .#rpc-diagnostics -- \
  --config onchain_pipeline.json --index-db state/onchain/index.sqlite \
  --output /tmp/freya-v3-mainnet-new.json --allow-mainnet --workers-stopped \
  --max-requests 10 --deadline-seconds 180
```

Use a new report filename each time. Do not proceed to sustained paper acceptance
until diagnostics/builds pass and actual mainnet failures are understood. Then
resume the existing paper session with `nix run --no-write-lock-file .#paper`.
Observe contiguous checkpoints, causal USD data, completed candle windows, genuine
paper outcomes and eligible learning. Passing synthetic tests cannot substitute
for these observations. Preserve the existing downstream acceptance criteria.

Rollback: stop owned workers/network session, build the previous reviewed package
and host revision, and restore the previous NixOS generation with
`sudo nixos-rebuild switch --rollback` if needed. Resume only manually authorized
workloads. Preserve current journals, configs, provider history, grants and pending
signatures; never restore an older database over new trades. No data migration or
budget reset is introduced.

### Validation evidence

The protected offline run passed 629 Python tests with zero failures, errors or
skips; privileged kernel tests were deliberately omitted, so its overall host
acceptance flag remains false. Ten focused diagnostic/probe checks also passed
after the final closed-client metadata adjustment. Rust passed 13 unit tests,
including separate stalled-loopback request and TLS-connect timeout cases.
The first full run exposed 11 parity-test fixture failures caused by the earlier
opener-reuse change; the fixture now varies responses on the same opener while
retaining all malformed-envelope assertions, and subsequent runs passed.

The final `native-rpc-app` built successfully from the complete public-source
staging snapshot, and its packaged diagnostics CLI passed a `--help` smoke check.
The NixOS VM build was attempted
offline but stopped when evaluation required 3,749 uncached derivations; no VM
or host/kernel lifecycle acceptance was obtained. Initial sandbox restrictions
also blocked local sockets; authorized loopback tests were rerun outside that
sandbox. Source-only Nix builds used a public-file staging snapshot so new
untracked modules were included without copying credentials/state.

Local evidence: `/tmp/freya-v3-offline-release.json` (629-test protected run),
`/tmp/freya-v3-offline-final.json` (initial failing run), and
`/tmp/freya-v3-offline-recheck.json` (626-test successful recheck). They are local
validation artifacts, not evidence from Monolith. No mainnet probe, sustained paper run,
new paper trade, ML promotion, or funded live activation was performed here.

## Mainnet reliability finding — November 2, 2026 (pre-repair baseline)

The operator reports frequent TCP drops on Monolith's external mainnet RPC path,
without observed HTTP throttling responses. The supplied logs repeatedly show
`native transport unavailable; endpoint omitted`, no HTTP status, retry delays of
10/20/40 seconds, and an unchanged finalized checkpoint (464670679). Paper entries
and learning remain blocked by unavailable fresh market evidence.

These logs establish repeated transport failures, not packet loss or its cause.
No HTTP 429 was reported in this excerpt; provider/network-level connection
limiting remains a hypothesis even without an HTTP response. At the time of the baseline, Rust mapped
request-send and response-body errors to the same generic connection exception;
local cancellation and unavailable client state also use that message. The retry
delays alone do not identify throttling.

Investigate mainnet reliability before further loopback throughput optimization:

1. Retain endpoint-redacted failure phase, elapsed time, timeout/connect/body
   classification and safe socket error codes. Distinguish cancellation and closed
   clients from network failures; never emit credential-bearing URLs or raw error
   chains that may contain them.
2. On Monolith, run an operator-controlled, bounded read-only probe against the
   existing reviewed mainnet endpoint. Target ten successful logical requests,
   with a fixed attempt/time ceiling rather than retrying until success. Count
   failures and setup requests, retain the shared pacing ledger, and honor every
   cooldown and Retry-After. Do not run competing collection during comparison.
3. Compare Python and native clients sequentially under matched request, timeout
   and pacing conditions. Record request method, finalized slot, response size,
   status/error category and duration. Validate authentic finalized getBlock
   receipts through the existing decoder/admission rules; a successful getSlot
   or JSON parse does not establish correct block ingestion or learning eligibility.
4. Correlate timestamped failures with bounded, operator-reviewed host/namespace
   TCP retransmission/reset counters, interface drops and network-budget status.
   Check DNS/TLS and stale pooled connections as hypotheses. Aggregate kernel
   counters alone cannot attribute packet loss to this RPC connection.
5. Separately observe sustained contiguous checkpoint progress and freshness under
   normal budgets. A short successful probe cannot establish sustainable ingestion.

Loopback fixtures test local transport behavior only. Their server-side split-write,
TCP_NODELAY and coalescing results do not diagnose mainnet TCP failures. Preserve
all existing risk controls, byte budgets, checkpoints, provider backoff and live
authorization. No mainnet probe, packet capture or host/network change was executed
for this findings update; the diagnostic probe described above is not yet implemented.

## Static code audit — possible failure locations

Reviewed the current checkout without executing tests, builds, network calls or
kernel commands. These are source-backed candidates recorded before the V3 instrumentation, not a confirmed mainnet root
cause. Line references describe this checkout; code changes may shift them.

| Priority | Location | Finding and discriminating evidence |
| --- | --- | --- |
| 1 | `native/solana_binary/src/rpc.rs:89–90,101–103` | Request-send and response-chunk errors both become `offline()`. Send failure can occur before response headers; chunk failure occurs after an HTTP status was received. Record a redacted phase/category, timeout flag and safe underlying socket code to distinguish these paths. |
| 1 | `native/solana_binary/src/rpc.rs:91–103` | The code reads HTTP status before reading the body, but returns it only after the complete body succeeds. A failing HTTP 200 body therefore produces `http_status: null` in current logs. Null status does **not** establish that no HTTP response arrived. Preserve the known status and bytes received in error metadata. |
| 1 | `solana_rpc_compat.py:103–108,132–134,154–156` | Every native `ConnectionError` is charged to shared provider backoff as a synthetic 503, then re-raised without method/category attribution. This is internal accounting, not an observed HTTP 503 or proof of throttling. Attach safe method/phase metadata without clearing backoff. |
| 2 | `native/solana_binary/src/rpc.rs:123–125` | A 15-second connect timeout and 30-second overall request timeout bound full finalized-block downloads. Slow or repeatedly retransmitted bodies may hit the deadline without HTTP 429. Compare elapsed failure time with these deadlines; do not increase them before measuring. |
| 2 | `onchain_ingest.py:233–237` and native body loop | Requests retrieve full JSON transactions, optionally two blocks per HTTP request. Request-count pacing does not bound response size or instantaneous packet rate. Compare single-block versus two-block failures, bytes received, elapsed time and verified response sizes under the same existing budgets. Never skip blocks or reduce validation to improve apparent reliability. |
| 2, isolated mode only | `solana_network_budget.py:127–171`; `solana_network_lifecycle.py:125`; `nix/solana-network-isolation.nix:26–27` | The installed namespace rules intentionally **drop**, rather than queue, packets exceeding configured rates. Defaults are 16 kbytes/s in each direction; exhausted quota or an expired active lease also falls through to drop. This is an actual code location capable of causing local packet loss and retransmissions without HTTP throttling. Establish that the process is in this managed namespace and inspect counters/lease before attributing failures to it. Keep all caps intact. |
| 2 | `native/solana_binary/src/rpc.rs:125`; `solana_rpc_compat.py:85–92` | One persistent client retains pooled connections for up to 30 idle seconds. Provider/NAT idle expiry could race reuse after a cooldown. This is a hypothesis: compare first request after idle versus subsequent requests, reset/EOF categories and connection-level observations. A transport failure alone does not prove a stale pool or justify bypassing pooling. |
| 3 | `native/solana_binary/src/rpc.rs:125` and `native/solana_binary/Cargo.toml` | Native HTTP explicitly disables proxies and uses the Rust TLS stack; Python urllib has a different proxy/TLS setup. A required proxy or trust-store difference can make the backends behave differently before any block is decoded. Compare redacted proxy-presence flags and TLS/connect classification; never print proxy credentials or disable certificate checks. |
| 3 | `native/solana_binary/src/rpc.rs:79–87,157–161`; `solana_rpc_compat.py:136–139`; `run_onchain_indexer.py:125–126` | Closed client state, a poisoned client lock and explicit cancellation share the same `offline()` message as network failures. Normal runner cleanup closes clients in `finally`; it is not evidence of a per-scan close bug. Correlate shutdown/restart timestamps and cancellation classification before treating this as TCP loss. |

### Applicability and retry interpretation

`solana_rpc_compat.py:33–54` accepts reviewed remote-provider mode without the
managed-node network-budget status and rejects a node-budget configuration in that
mode. The namespace-policing candidate is therefore conditional, not the default
explanation for a normal external-provider run. Independent host/router rules or
a process launched inside a restricted namespace still require host evidence.

`onchain_ingest.py:337–350` derives increasing reconnect delays locally and stops
after the bounded failure count. With a five-second base interval, 10/20/40-second
pauses arise from that exponential formula. They are not measurements of dropped
packets or provider Retry-After. The saved checkpoint is deliberately retained.
`ProviderBudget.failure(...,503)` also records local backoff; its synthetic status
must not be presented as a response from the server.

The one-request native semaphore rejects overlapping operations with an explicit
`RuntimeError("HTTP backpressure...")` (`rpc.rs:82`). Size/encoding violations use
`ValueError` and fail-closed evidence handling. Those are separate errors from the
reported generic `ConnectionError`; current logs do not implicate them.

First implementation target: retain safe failure phase, request method, known HTTP
status, elapsed duration, bytes received and timeout/connect/body categories across
the Rust/Python boundary. This would make the existing bounded mainnet probe able
to distinguish the leading candidates. Do not log raw endpoint-bearing errors,
change provider quotas, disable network isolation or silently switch backends.

## Implementation scope

This change reuses the Python HTTP opener across calls, avoids redundant native
identity-response UTF-8 round trips, and preallocates Rust response buffers from
Content-Length with a 1 MiB eager-allocation cap. The 64 MB response bound remains.
No production pacing, retries, method admission, namespace controls, result
validation, source permissions or live authorization change. HTTP opener reuse is
not a claim of Python TCP connection pooling.

The existing benchmark fixture and all A/B cases remain unchanged. In particular,
server-side TCP_NODELAY/coalescing remain experimental server configurations;
client changes cannot force external providers to adopt them. No improved speed,
external RPC reliability or ingestion capacity has been measured for this revision.

## Operator checks

From the reviewed checkout, with Nix available and the existing reviewed Cargo.lock:

```bash
nix develop --no-write-lock-file .#native-rpc --command python -m unittest discover -s tests -p test_rpc_response_efficiency.py
nix develop --no-write-lock-file .#native-rpc --command python -m unittest discover -s tests -p test_block_batch.py
nix develop --no-write-lock-file .#native-rpc --command python -m unittest discover -s tests -p test_solana_rpc_compat.py
nix build --no-write-lock-file .#native-rpc-app
nix develop --no-write-lock-file .#native-rpc --command env SOLANA_NATIVE_LOOPBACK_STRESS=1 python -m unittest discover -s tests -p test_rpc_transport_complete.py
nix develop --no-write-lock-file .#native-rpc --command python benchmark_rpc_transport.py --allow-loopback --ab --iterations 100
```

The overhead-only implementation originally left these checks to the operator.
See V3 validation above for checks run during the subsequent reliability repair. Compare identical
cases on the same host, retaining full output and source revision for before/after.
Do not compare local getSlot fixtures with external-provider getBlock latency.

No service activation is automatic. After validation, stop only owned workers and
resume the existing paper session with `nix run --no-write-lock-file .#paper`.
Live activation remains subject to the existing operator-authorized workflow.
Monitor checkpoint progress, lag, cooldowns and transport failures independently
of fixture throughput. For rollback, stop owned workers and rebuild the previous
reviewed revision. Preserve all operational state, provider ledgers, configs,
model history and pending signatures; this change introduces no data migration.
