# V3 runtime acceptance: blocked on deployed event contract

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
