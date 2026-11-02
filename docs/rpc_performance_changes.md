# Client overhead reduction

## Mainnet reliability finding — November 2, 2026

The operator reports frequent TCP drops on Monolith's external mainnet RPC path,
without observed HTTP throttling responses. The supplied logs repeatedly show
`native transport unavailable; endpoint omitted`, no HTTP status, retry delays of
10/20/40 seconds, and an unchanged finalized checkpoint (464670679). Paper entries
and learning remain blocked by unavailable fresh market evidence.

These logs establish repeated transport failures, not packet loss or its cause.
No HTTP 429 was reported in this excerpt; provider/network-level connection
limiting remains a hypothesis even without an HTTP response. Rust currently maps
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
kernel commands. These are source-backed candidates, not a confirmed mainnet root
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

The coding agent did not run these tests, builds or benchmarks. Compare identical
cases on the same host, retaining full output and source revision for before/after.
Do not compare local getSlot fixtures with external-provider getBlock latency.

No service activation is automatic. After validation, stop only owned workers and
resume the existing paper session with `nix run --no-write-lock-file .#paper`.
Live activation remains subject to the existing operator-authorized workflow.
Monitor checkpoint progress, lag, cooldowns and transport failures independently
of fixture throughput. For rollback, stop owned workers and rebuild the previous
reviewed revision. Preserve all operational state, provider ledgers, configs,
model history and pending signatures; this change introduces no data migration.
