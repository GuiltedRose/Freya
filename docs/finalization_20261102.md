# Runtime finalization and acceptance

Latest status: [ingestion repair](ingestion_repair_20261102.md). The published SDK
contract resolves the eight-byte event mismatch. Current mainnet samples decode,
but the bounded paper run encountered public-RPC throttling; sustained fresh,
complete ingestion and paper lifecycle acceptance remain unverified. The earlier
validation evidence below is preserved as historical evidence.

For this project, **production-ready means the shared bot pipeline works reliably
in paper trading**: native RPC discovery → current market data and candles →
momentum-reclaim evaluation → paper entries and exits → persistent outcomes and
learning, with working monitoring and restart recovery.

Paper and live use the shared tools, strategy, models and rules, with mode-specific
restrictions and concurrency allowances. Working paper validates that shared
pipeline. Live signing, actual fills and order reconciliation have additional
execution requirements; funded-live validation is separate from this paper readiness
target and remains under the operator's control.

Production-ready does **not** mean profitable or guaranteed to make anyone money.
Results depend on the coins each bot discovers, market conditions, execution costs
and operator configuration. Passing tests or starting the dashboard alone does not
establish a working paper pipeline or a profitable strategy.

This checkout has not yet met that production-readiness target. The original event
layout blocker below has been repaired; the latest report distinguishes that repair
from remaining provider-capacity limits and uncompleted paper acceptance.

## Earlier confirmed blockers and repairs

* **Event layouts:** read-only replay of 128 saved finalized transactions produced
  identical Python/Rust/compare results: 127 transactions had issues, with repeated
  eight-byte tails beyond pinned fields. No receipts were modified. Unknown bytes
  are still quarantined. The bundled IDLs match the published field lists; upstream
  also has an [open layout report](https://github.com/pump-fun/pump-public-docs/issues/54).
  An exact serialization contract matching the deployed program, plus replay
  acceptance, is required before these events can supply production markets.
* **RPC pacing:** built-in profiles inherited a five-second delay on every request.
  They now distinguish `request_interval_seconds` (default 0.5) from loop idle rest.
  Explicit custom settings, the shared minimum, cooldowns, denial records and
  self-hosted namespace/byte-budget enforcement remain active. No checkpoint jumps.
* **Native bandwidth:** the new encoded HTTP methods negotiate gzip and pass bounded
  compressed evidence to the same strict decompressor as Python. Unknown encodings,
  truncation, trailing data and decoded-size overflow fail closed. Legacy native
  methods remain compatible. Compression needs validation with a freshly built
  extension; a cached extension cannot validate this new code.
* **Wallet leads:** paper/live index-backed runs use `IndexedWalletFeed`. They no
  longer need a detached wallet collector just to follow the same indexed program
  trades. Leads require fresh, covered, verified index data, are bounded to 1,024
  records, and never certify independent humans, net wallet spend or training labels.
  Existing saved-wallet history and the separate explicit collector are preserved.
* **Live recovery:** the runner supplies approved source/oracle config separately
  to exit management. If market indexing is unavailable, an existing holding can use
  identity-checked direct account/liquidation quotes and a fresh verified oracle.
  Recovery snapshots cannot authorize entries or market-learning labels. Pending
  orders, inventory discrepancies, unsupported migrations, bad account layouts,
  transport loss and oracle failure remain blocked/reconciled, never guessed away.
* **Capital:** percentage warmup stays at the minimum allocation tier until supported
  Kelly evidence exists. Performance tiers remain independent; unsupported edge does
  not amplify the initial budget. Account decoders reject unknown nonzero extensions.
  The coordinated paper runner explicitly disables automatic balance/run resets:
  a risk halt must not silently archive outcomes, refill capital or erase learning
  eligibility. Manual reset remains a separate deliberate raw-CLI operation.
* **Visibility:** quarantine reasons appear explicitly in required-hour health checks;
  wallet source reasons are printed instead of a generic unavailable label. Existing
  paper/live dashboards and family-level historical ML metrics remain visible.
* **Nix privacy:** aggregate validation uses Git-backed source references, not
  `path:` copying of a working directory containing ignored secrets/state. It builds
  only the native app and offline indexer check, excluding NixOS VM/node/network jobs.
  New source files must be committed or staged before Git-backed flake builds.

## Validation evidence and exclusions

Final results are recorded in [validation evidence](validation_2026-11-02.json):

* Protected Python/native-loopback suite: **584 passed**, zero failures/errors/skips.
  Operational state, credentials and external networking were blocked.
* Rust: **9 passed**, zero failures; no doctests defined.
* Nix: native extension and complete native app built; **143 offline indexer tests
  in 13 suites passed**. Paper/live CLI help, packaged recovery imports, and edited
  service-module syntax passed. A public-only source snapshot included new files
  without copying `.env`, wallet, state or model data or changing the Git index.
* Public-mainnet acceptance probe: **6 strictly read-only paced requests**, 2.60 s,
  409,005 response-body wire bytes versus 1,451,933 decoded bytes. Mainnet identity,
  gzip transport and the current oracle validated. **All 33 relevant transactions
  in the sampled finalized block had decoding issues.** No wallet was loaded and
  no transactions were submitted. Only the normal provider pacing ledger advanced.

The successful probe demonstrates connectivity, not sustained ingestion throughput,
complete market history or readiness to trade. The layout blocker is reproduced
against current mainnet, not only old fixtures. At that validation point, paper production readiness was blocked by the feed issue;
the later SDK repair and current provider-capacity findings are linked above. Funded-live execution has not been validated and remains
off under the operator's instructions; it is not a prerequisite for accepting the
shared paper pipeline. Self-hosted validator startup and NixOS/kernel network enforcement were
not run, as requested. Offline mocked policy/config tests are not node acceptance.

SVM risk annotations, sufficient chronological LSTM windows, and independent
retrieval outcomes remain genuine data requirements. Empty families report blocked
or collecting; no labels or qualification are manufactured. Candidate IDs are
explicit selections, not automatic live promotions. Import workers retain their
documented bounded-evidence limits; exceeding them needs a reviewed paged workflow.

## Operator acceptance

Stage/commit new source and test files, never private configs, `.env`, state or the
ignored local `flake.lock`. Keep an existing validation report; choose a new output
filename for each run.

```bash
nix --extra-experimental-features 'nix-command flakes' develop \
  --reference-lock-file ./flake.lock --no-write-lock-file .#native-rpc \
  --command python validate_production.py --native --nix \
  --output /tmp/crypto-bot-final-acceptance.json
```

Do not enable kernel tests or build the self-hosted validator as part of this check.
`requested_checks_passed` describes the selected checks; `live_ready` remains false
and is a separate funded-execution status, not the definition of paper production
readiness.

Paper acceptance requires observed discovery, fresh complete market/candle data,
correct strategy decisions, simulated entries and exits when signals qualify,
persistent eligible outcomes, and learning updates when sufficient evidence exists.
Also verify interruption/restart preserves balances, positions and learning state,
and that risk/data vetoes and independent allocation/concurrency tiers behave as
configured. Do not force trades or manufacture labels to satisfy acceptance. Neither
a winning session nor a profitability guarantee is required to demonstrate operation.

After tests/builds pass, inspect configuration offline:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --state-dir state/paper_volume --preflight
```

Then resume the existing paper session with the same command without `--preflight`.
Use `--initialize` only if no session exists. Keep paper running only while inspecting
freshness, actual decoder issues, oracle coverage, latency and worker health.
Do not interpret an increasing number of quarantined blocks as usable history.

For an independently supervised shared feed, use `.#data`; consumers then use
`--external-workers`. Live uses the same supervisor with `.#live -- --execute`, but
it is not part of this acceptance run and should remain off while the feed blocker
exists. A fallback exit quote is never proof that new entries are safe.

To roll back software, stop owned services with Ctrl+C, preserve every state/config/
model file and backup, and rebuild the prior reviewed revision. These repairs add no
database schema migration. Do not reset indexes to conceal bad history or overwrite
a paper session to resolve a host/path mismatch. Previously quarantined receipts
require explicit independent replay after a supported schema becomes available;
they are never silently relabeled or marked verified.
