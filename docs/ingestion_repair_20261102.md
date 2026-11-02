# Ingestion repair and acceptance

The software defects found in the investigated ingestion path are repaired. This
is **not paper production acceptance**: the public endpoint throttled sustained
collection, so complete fresh windows, observed paper trades and resulting learning
have not been demonstrated. Historical reports are preserved; current evidence is
in [the repair validation report](validation_ingestion_20261102.json).

## Root causes and repairs

The public-docs IDLs were stale relative to the published official SDK artifacts.
The integrity-verified [Pump SDK 4.3.1 archive](https://registry.npmjs.org/@pump-fun/pump-sdk/-/pump-sdk-4.3.1.tgz)
defines the missing `quote_transfer_fee:u64` after `creator_fee_unclaimed` in
TradeEvent, BuyEvent and SellEvent. PumpSwap SDK 2.2.0 independently carries the
same added event field. It represents the quote-token transfer fee, not padding.
The bundled IDLs now pin that exact SDK contract; manifests retain archive/file
hashes, integrity, package revision and the previous public-docs pins.
Python and Rust still reject unconsumed bytes and malformed/truncated layouts.
Native SOL cannot acquire a fabricated Token-2022 quote transfer fee.

Other failures exposed after fixing that field were repaired separately:

* Explicit legacy instruction forms record omitted trailing flags as absent, never
  as invented false values. Documented creation EOF fields remain distinct from
  required or partially supplied fields.
* Borsh byte vectors now decode identically with bounded lengths in both backends.
* Multi-hop receipts bind to canonical pool/curve addresses and five-account hop
  groups. Internal curve calls require their authenticated PumpSwap parent; each
  hop requires exactly one primary receipt, a consistent direction and continuous
  mint path. Middle routing legs do not become wallet-buying evidence.
* Protocol buy-and-burn uses its derived boost authority, paired burn receipt and
  amount/reserve checks. Its pool volume remains observable; its PDA is not a
  watched buyer or an independent human.
* TransferChecked supplies its mint/decimals even when a temporary WSOL account is
  closed before final balances. Conflicting balance evidence remains rejected.
  Self-transfers are marked with zero credited/debited movement.
* The explicit Token-2022 TransferCheckedWithFee format retains its checked fee.
  Unchecked/ordinary Token-2022 transfer fees are not assumed zero. Unknown
  extensions remain quarantined. Receipt support does not enable unsupported
  execution-account extensions or mint risk checks.
* Python/native transport supports at most two getBlock calls per HTTP request,
  with strict complete/unique ID matching, original error types and per-block
  contiguous commits. HTTP pacing, cooldowns, namespace checks and byte bounds
  remain active. Built-in profiles select two; a reviewed endpoint can explicitly
  retain `block_batch_size: 1` if it does not support batches. That choice does not
  guarantee sufficient throughput for current full-block ingestion.
* Partial prefix progress is published even when a later request fails. Long local
  cooldown checks no longer consume the bounded network-retry budget. Diagnostics
  expose the failing method, HTTP status and actual cooldown deadline. Missing USD
  conversion now surfaces its persisted oracle failure rather than only warmup.

The underlying event semantics are described by the pinned SDK artifacts; route
account groups and per-hop volume are also documented in the official
[multi-hop contract](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/MULTI_HOP_SWAP.md).
Checked token identities/fees follow the official
[SPL Token interface](https://github.com/solana-program/token/blob/main/interface/src/instruction.rs)
and [Token-2022 fee interface](https://github.com/solana-program/token-2022/blob/main/interface/src/extension/transfer_fee/instruction.rs).
No data-source permissions, model qualification, risk limits or live authorization
were relaxed. Momentum reclaim and native RPC discovery remain in place.

## Evidence and remaining limit

The protected Python/native suite passed 605 tests, with no failures, errors or
skips. The deliberately excluded kernel suite was not run. Rust passed 11 tests.
The final Nix app and native extension built; 158 tests in 14 offline suites
passed. The packaged audit replayed all 128 sampled receipts cleanly. Exact
artifact paths and source hashes are recorded in the JSON report.
No self-hosted validator build/startup or NixOS/kernel network runtime was tested.

The fresh two-block native RPC sample decoded all 90 relevant transactions,
producing 99 events without decoder issues. A separate read-only historical audit
replayed 128 original receipts cleanly with identical Python/Rust/comparison
interpretations. The earlier bounded paper capture's 296 retained transactions also
replayed cleanly after its additional route/fee repairs. Decoding agreement is not
source/deployment admission, verified wallet net cashflow or training qualification.

The packaged coordinated runner configured and started indexer, paper and learning
workers with a new isolated virtual session. It indexed eight blocks and 376 events
without block quarantines. Interruption/restart retained capital/tier state and
existing block rows, and advanced the saved checkpoint. This was an empty-position
restart observation; held-position recovery is covered separately by deterministic
tests. All owned acceptance services were stopped.

Sustained requests received HTTP 429. The test therefore did not establish ingestion
throughput sufficient to keep up, complete candle windows, observed paper entries
and exits, or learning from new completed outcomes. This is a provider-capacity
blocker, not an absence of a naturally qualifying signal. The public RPC is
[rate-limited, with limits subject to change](https://solana.com/docs/references/clusters).
Use a permitted source with sufficient history and sustained full-block capacity;
never clear cooldowns, rotate endpoints to evade limits, skip slots, substitute
future oracle prices or manufacture trades to satisfy acceptance.

## Apply the schema/configuration update

Stage/commit all new public source/fixture files before Git-backed flake builds.
Keep `.env`, private configuration, state, models and the ignored `flake.lock` out
of the commit. Recheck an existing configuration's deployment pins with the runner:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --state-dir state/paper_volume --configure

nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --state-dir state/paper_volume --preflight
```

Configuration setup uses read-only RPC probes, retains exact configuration backups
and does not reset paper or index state. An old IDL pin is an explicit preflight
error. Changing the source still requires its existing permission review. Once the
source has adequate capacity, resume the same session:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --state-dir state/paper_volume
```

Use `--initialize` only for a deliberately new session. Verify fresh complete
coverage, causal USD conversions, final candles and genuine strategy decisions;
then observe actual paper entries/exits and eligible learning when evidence exists.
A short smoke run and passing tests are insufficient for that acceptance.

## Read-only historical recovery audit

The new packaged audit replays a bounded page of original hash-checked receipts.
It creates a new report, changes no database and never promotes old quarantine,
learning labels or source rights:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#audit-ingestion -- --db state/onchain/index.sqlite --limit 1000 \
  --output /tmp/freya-ingestion-audit-new.json
```

Choose a new report filename each time. Use the returned `after.slot` and
`after.tx_index` with `--after-slot` and `--after-index` for the next page. This
checks receipt decoding, not complete-hour coverage or deployment/source admission.
Old quarantined blocks remain quarantined until an explicit independently reviewed
replay process admits them; do not erase their issues or reset the index to hide
history. Initial missing historical USD observations cannot be backfilled from a
current price.

To roll back, stop only owned workers with Ctrl+C, preserve all state/config/model
files and backups, and build the prior reviewed revision. Schema hash mismatches
remain explicit. These repairs add no database schema migration, capital refill,
checkpoint jump or automatic relabeling. Keep funded-live activation under the
operator's control.
