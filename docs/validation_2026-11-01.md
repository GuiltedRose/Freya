# Bot validation: 2026-11-01

Initial bot tests used one-time authorization. The user subsequently authorized
non-NixOS Nix checks, then narrowed execution to the node. The update below records
the node-specific results; NixOS activation/VM checks were not run.

| Check | Result |
| --- | --- |
| Full Python bot suite with native RPC and loopback HTTP/WS tests | 536 passed, no failures/errors/skips; kernel suite separately excluded |
| Rust `cargo test --locked --features rpc-engine` | 9 passed; no doctests defined |
| Targeted integration/regression rerun during fixes | 88 passed |
| Python syntax parsing | All 173 source/test files parsed |
| `git diff --check` | Passed |
| Isolated kernel quota/lease tests | 2 failed at nftables installation; DSCP rewrite expressions returned `Operation not permitted` in rootless namespaces |
| Agave Nix package | Release build passed; version and packaged CLI options executed successfully |
| Isolated local node startup | Packaged `solana-test-validator` reported RPC health `ok`; stopped afterward |
| Production Agave startup against isolated fixture | Executable launched, then failed OS limits; stopped; not a mainnet sync test |
| NixOS service activation/VM integration | Not tested |
| External RPC discovery/indexing and sustained real workload | Not tested; no reviewed runtime source configuration supplied |

Python 3.14.8, solders 0.29.0, websockets 15.0.1 and zstandard 0.25.0 were used.
Python dependencies came from the existing local cache, extracted only into `/tmp`.
Cargo's missing dependencies were fetched into a separate `/tmp` Cargo home. Native
compilation used `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` for this host's Python;
this does not validate the flake's separate Python 3.13 environment or Agave build.

The unmodified standard-library `asyncio.to_thread` smoke check stalled during
executor shutdown here. The complete test run used a **test-process-only 10 ms
event-loop pulse** to keep executor wakeups scheduled. No production scheduler
code was changed. Repeat the unmodified suite on the deployment host; this test
environment workaround is a validation limitation, not a demonstrated bot fix.

The kernel tests ran under disposable user/mount/network namespaces, with private
`/run` and no external routes. Host firewall/routing was not changed. Failures are
retained, not skipped or worked around by removing DSCP, quota or lease controls.
Full packet enforcement remains unverified until these tests pass with the
deployment host's appropriate kernel capabilities/modules.

## Repairs made during validation

* Corrected the Zstandard decompressor's maximum-window setting; added a valid
  40 KB frame regression alongside oversized-output rejection.
* Added missing modeled fixed network costs to native-indexed paper observations.
  The end-to-end finalized-RPC → candles → paper entry → restart → exit → learning
  test now passes. Paper fees remain modeled, not verified executable quotes.
* Made paper outcome reporting tolerate journaled non-object malformed snapshots.
* Updated fixtures to include required receipt signatures, measured liquidity and
  the current multi-position store interface. Updated stale-price assertions to
  require rejection rather than weakening freshness controls.
* Updated a stale rollback-reason assertion and an obsolete CLI assertion that
  predated explicitly authorized native-data live execution. Unsigned input still
  cannot load a signer. Kernel failures now expose nftables diagnostics.

Detailed session artifacts, retained separately without overwriting earlier runs:

* `/tmp/crypto-bot-validation-20261101-native.json` and matching `.log`
* `/tmp/crypto-bot-rust-validation-network.log`
* `/tmp/crypto-bot-kernel-validation-details.log`
* `/tmp/crypto-bot-validation-runner.py` (the exact temporary scheduler workaround)

No operational databases, wallet records, models, or checkpoints were reset or
deleted. An owner-only, gitignored `.env` was created from the example; signing
fields remain empty and the RPC URL remains a placeholder. This file does not
grant storage/training permissions or initialize billing accounting.

## Node-only follow-up

`nix build .#agave-node` completed, and `nix run .#agave-node -- --version`
returned Agave 4.3.0. The pinned resource/compression options also parsed through
the executable's help path. Fixed two confirmed Nix integration defects: missing
`meta.mainProgram = "agave-validator"`, and an undersized dynamic-port range.

A local validator fixture ran in a private network namespace with no external
routes, explicit temporary configuration/mint, and new `/tmp` ledger/identity
files. It returned health `ok` at slot 0; block production/sustained replay was
not established. It was stopped before the production executable was launched
against that fixture. The production executable failed its OS buffer-limit check;
memory-lock allowance was also below its requested value. No OS check was disabled.
Added matching NixOS sysctl defaults and service memory-lock allowance for future
operator activation. This Arch environment cannot apply the privileged settings.

All smoke-test processes stopped. Existing operational state was untouched.
Scratch fixture state/logs are retained under `/tmp/crypto-bot-agave-startup-*`.
Latest report: `/tmp/crypto-bot-agave-startup-result.json`; build log:
`/tmp/crypto-bot-nix-agave-resume.log`. These results do not establish zero issues
in production or readiness for mainnet synchronization.

## Activation remains blocked

The user authorized collection, paper trading and learning, **not real-money
trading**. No production services were started. This checkout lacks a reviewed
real RPC/source configuration, initialized bandwidth ledger and deployed runnable
service environment; `.env` alone does not supply those prerequisites. A new paper
run also needs explicit virtual inventory. No source approval, billing-cycle usage,
training labels or starting inventory were invented to make startup succeed.

Use [challenger operations](challenger_operations.md) and
[self-hosted RPC operations](selfhost_rpc.md) for operator build/activation/rollback
steps. Passing synthetic/loopback tests does not establish profitability, live
readiness, or feasibility of mainnet bootstrap within the transfer allowance.
