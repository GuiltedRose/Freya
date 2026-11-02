# V3 runtime acceptance: blocked on deployed event contract

**Historical report:** the later [ingestion repair](ingestion_repair_20261102.md)
identifies the missing published SDK field and resolves the serialization mismatch.
The earlier findings and test counts below are retained unchanged as historical
evidence; they do not describe the current decoder's admission results.

Production readiness means reliable end-to-end paper operation, using the shared
strategy, models and rules. This acceptance has **not passed**. No profitability
or funded-live claim is made. Full evidence is in
[the new V3 report](validation_2026-11-02_v3.json); previous reports remain intact.

## Serialization investigation

The official current Pump and PumpSwap type definitions were fetched and compared
with the bundled IDLs. Every pinned type definition is identical. The GitHub file
blob IDs are recorded in the report. TradeEvent has 35 fields, BuyEvent 40 and
SellEvent 33; all already include `creator_fee_unclaimed:u64`.
The [October 7 candidate](https://github.com/pump-fun/pump-public-docs/issues/56)
therefore does not resolve this checkout's mismatch.

[Anchor's event implementation](https://github.com/coral-xyz/anchor/blob/v0.31.1/lang/attribute/event/src/lib.rs)
serializes an event discriminator followed by Borsh fields. The event-CPI macro
prepends a separate instruction tag. It does not define an eight-byte trailer.
This is a framing reference, not proof of Pump's deployed serialization contract.

A read-only replay of 128 saved transactions through Python, Rust and comparison
backends gave identical results. Of these, 127 had issues. All 131 successfully
parsed diagnostic trade prefixes matched their Program data log bytes exactly
after removing the separate CPI tag, and all still had eight unconsumed bytes.
All 131 timestamps matched block time. The remainder was zero in this sample;
that does not establish padding or prove where a field was added or widened.
Other transactions also contained truncated fields. No original receipt changed.

Eleven single-prefix curve token balance deltas matched the published prefix's
token amount. Seven available native wallet deltas did not reconcile with the
simple amount-minus-published-fees formula. Those checks did not isolate other
transfers, rent, routing or fee semantics. They establish neither a new decoder
contract nor verified SOL cashflows. They cannot authorize event admission.

The [upstream report](https://github.com/pump-fun/pump-public-docs/issues/54)
also contains later reproductions after `creator_fee_unclaimed` was included.
These are issue reports, not maintainer attestations of a replacement contract.
No verified meaning, ordered type layout or deployment range for the remaining
bytes was established during this investigation. Accordingly no speculative
Python/Rust compatibility layout was added and quarantined events remain excluded.

## Additional validation

The six native event/projection tests passed with zero failures, errors or skips.
A new regression checks both recorded finalized fixtures against log/CPI framing,
all three decoding backends and strict rejection. It does not remove unknown bytes
or turn diagnostic prefixes into market observations or labels.

The fresh bounded native mainnet probe made six paced read-only requests:

* Finalized slot: `464446876`; 333 transactions; 28 relevant transactions.
* Accepted relevant transactions: **0**; all 28 had decoding issues.
* Response-body bytes: 844,219 wire, 3,141,540 decoded; elapsed 2.56 seconds.
* Mainnet identity, compressed transport and oracle checks passed.
* No signer loaded, transaction submitted or bot service started. Existing provider
  pacing accounting advanced normally; no index or paper state was changed.

This confirms the feed blocker on current mainnet. It does not measure sustained
indexing throughput, current ingestion lag, complete candles or strategy operation.
Earlier Python/native, Rust and Nix results are retained in
[the previous validation](validation_2026-11-02.json), not presented as new runs.
No Rust/application source changed in this V3 investigation, so their builds were
not repeated. Self-hosted validator and NixOS/kernel runtime checks remain excluded.

## Remaining acceptance and smallest safe continuation

First obtain an authoritative or independently verifiable complete ordered event
contract, with units/fee semantics and its deployed revision or applicability range.
Then pin it, add exact Python/Rust fixture coverage and independently reconcile
economic amounts. Replaying into an isolated audit result must leave original raw
receipts intact; production quarantine history must never be silently relabeled.

Only after that passes, run the existing coordinated paper pipeline and measure
actual throughput, lag, discovery, USD conversion and complete fresh candles.
Observe genuine qualified entries and exits, eligible persistent outcomes and
learning updates when evidence is sufficient. Verify interruption/restart of that
session without refills or resets. Existing deterministic integration tests are
useful evidence but do not replace these observations.

No paper observation session was started during this V3 pass because its verified
market input remains blocked. This is a component failure, not an absence of a
natural trading signal. Real paper lifecycle, new learning outcomes, sustained
restart acceptance and complete one-command runtime acceptance remain unverified.
No funded-live activation is needed for that paper acceptance.

## Indexer and compatibility byte trace

A subsequent regression specifically checked whether our indexer or compatibility
layer introduces the bytes. Both hash-pinned finalized fixtures were served through
real loopback HTTP sockets, using Python and Rust clients, identity and gzip bodies,
and fixed-length and chunked transfer framing: 16 combinations, 64 local requests.
The test passed. No external RPC or operator state was used.

The response passed through the real JSON-RPC parser, `Ingester.step`, comparison
decoder and `EventStore` canonical JSON/zlib persistence. Stored transaction objects
and their hashes matched the fixture inputs, including instruction data and log
strings. Stored block contents matched except for the intentional top-level
`_indexer_deployments` audit metadata. Direct decoding before ingestion and decoding
after storage returned identical events/issues. The compact compatibility projection
also retained rejection and produced no eligible rows. Deployment probes were mocked;
this tests byte preservation, not chain continuity or deployment/schema admission.

Static tracing confirms the polling indexer requests full JSON blocks and calls
`Decoder.transaction` directly. Native HTTP returns the original response body
(base64-wrapped only for gzip transport); Python validates/decompresses that body
before parsing JSON. Neither path inserts event fields. The separate binary-ingest
adapter is not called by this finalized JSON polling path. Event decoding starts
after the eight-byte CPI tag and eight-byte event discriminator in both Borsh
backends; their returned offsets and field values agree.

This rules out byte insertion or alteration in the exercised transport/index/storage
paths for these receipts. It does not prove that every deployed event has the pinned
schema, or establish the unknown bytes' meaning. The unresolved boundary remains
the event serialization contract versus decoder interpretation, not a demonstrated
transport/indexer corruption. Evidence is recorded separately in
[the byte-trace report](validation_2026-11-02_indexer_trace.json).
