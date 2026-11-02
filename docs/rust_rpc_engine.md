# Native RPC transport and compatibility

Rust owns bounded HTTP/WebSocket transport and generic Borsh field extraction.
Python owns source/deployment checks, transaction context, market normalization,
checkpoints, candles and learning. See [ingestion repair](ingestion_repair_20261102.md)
for executed validation and [production acceptance](production_acceptance.md) for
the remaining runtime acceptance requirements. No profitability is implied.

## Interfaces and limits

`solana_rpc_compat.make_rpc` preserves `IndexRpc.call`, `validate`, source identity
and telemetry. `backend=python` and conservative `auto` use Python; explicit `rust`
requires the feature-enabled extension and never silently substitutes a backend.
Both paths enforce the same read-only RPC/result contract. Native Borsh extraction
does not independently authorize a market observation or transaction.

Native HTTP uses one persistent Tokio runtime, releases the GIL for synchronous
calls and reuses its connection pool. `call_async` serves async consumers;
synchronous calls inside a running asyncio loop are rejected. Closing a client
cancels its active work and drops its pool without tearing down other clients.
Concurrent operations fail bounded backpressure rather than queueing indefinitely.

The transport permits TLS or explicitly opted-in literal loopback, rejects HTTP
redirects/environment proxies, and enforces bounded requests, 30-second HTTP and
15-second connect deadlines. Response bodies and decompressed output are capped
at 64 MB. Gzip is negotiated and strictly decompressed; unknown encodings,
truncation, trailing compressed data and overflow are rejected. Body-byte telemetry
is not total ISP/kernel traffic accounting. Full blocks can be batched in pairs;
response IDs must be complete and unique before contiguous commits proceed.

WebSocket send/receive halves have independent bounds; frames/messages are capped
at 2 MB. A quiet connection uses a 20-second idle ping and 10-second liveness wait.
Close interrupts pending work. Python's validated subscription/session layer owns
acknowledgments, deduplication, durable hints/gaps, reconnect and finalized catch-up;
Rust does not create a second recovery loop.

## Source permission, history and failover

Public remote-provider mode requires its reviewed HTTPS/source policy. Self-hosted
isolated mode additionally requires the fresh root-owned namespace/budget status.
The inode, expiry and budget permission must match before native I/O. Existing
kernel quotas, reserves, leases and accounting remain authoritative. See
[self-hosted RPC](selfhost_rpc.md) and [network lifecycle](nixos_network_lifecycle.md).
Live native transport on platforms without the required enforcement remains denied;
portable parsing/offline availability is separate.

All real indexer adapters probe `getFirstAvailableBlock` before and after obtaining
the produced-slot manifest. Pruned/unavailable history cannot resemble skipped
slots or advance the checkpoint. `minimumLedgerSlot` is diagnostic, not proof that
full blocks remain available. RPC manifests are provider evidence, not independent
consensus verification. Explicit approved transport/history failover resumes the
existing durable prefix; malformed evidence, unsupported versions and denied
sources do not trigger a backend substitution or checkpoint jump.

Optional WebSocket failover uses top-level `stream_failover: true` with at most
four uniquely identified enabled endpoints. Each requires reviewed storage/stream
policy, an explicit approved `ws_url`, matching network budget and successful
mainnet validation. Transport/reconnect exhaustion may switch to the next approved
source with explicit state/counters. Malformed notifications, authorization,
throttling and mismatched budgets stop rather than switching to evade a restriction.

Provider pacing, Retry-After and denials persist across restarts. Local cooldown
checks do not consume actual network-retry attempts. Cancellation records a gap;
unexpected listener exit and final-batch errors cannot be reported as success.

## Build, verification and measurement

Use the pinned Cargo.lock; ordinary builds do not require regenerating it. Stage
new public source files for Git-backed flakes, while keeping private state and the
ignored local flake.lock out of Git. Use [production acceptance](production_acceptance.md)
for aggregate checks and [pipeline recovery](pipeline_recovery.md) for deployment.
Node/kernel tests are separately selected operator actions.

`benchmark_rpc_transport.py --allow-loopback` measures identical local Python/Rust
HTTP fixtures, connection reuse and process RSS. It is not WAN or full-ingestion
performance. `benchmark_solana_rpc_replay.py ORIGINAL_CAPTURE.json` measures offline
RPC/base64/binding work using authentic binary captures. It cannot turn JSON-only
captures into original wire evidence. Unsupported instrumentation remains explicitly
unavailable; do not infer allocation improvements or profitability from these tools.

To roll back, stop owned workers, select a reviewed compatible application/config
revision and preserve the same databases, model history, archives and accounting.
Never roll a bandwidth ledger back with a software generation, zero a quota, erase
quarantine or move the checkpoint to the latest tip to make recovery appear healthy.
