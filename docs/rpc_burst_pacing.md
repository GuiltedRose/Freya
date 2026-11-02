# Shared external RPC burst pacing

External RPC clients allow ten paced HTTP requests, then a ten-second pause.
This is proactive pacing, not a claim about a provider's published quota or proof
that full-block ingestion can keep up. Existing per-request spacing, byte budgets,
401/403 denial and 429/Retry-After backoff remain enforced. A two-block batch counts
as one HTTP request; its two-logical-call cap remains unchanged.

Python and Rust indexers use the same limiter. Wallet observation and live RPC
also use it, including transaction submission: pacing never retries a submission.
A pause before submission is not a sent transaction. Preserve pending signatures
and reconcile ambiguity using the existing journal workflow.

The additive `provider_bursts` table lives in the existing provider-limits SQLite
ledger. All workers must share `CRYPTO_BOT_PROVIDER_LIMITS` (the same absolute file)
when running independently. Known public-mainnet aliases share a provider key;
other endpoints share by hostname, including distinct API-key paths. Counters and
pause deadlines survive restart; success does not clear them. This limiter covers
these clients, not unrelated programs or other machines sharing the public IP.

Index endpoints accept `burst_requests` (integer 1–100) and
`burst_cooldown_seconds` (1–3600), defaulting to 10 and 10. The public Solana aliases
cannot exceed ten or shorten the ten-second pause. Wallet/live clients use the
10/10 default. Isolated self-hosted index endpoints and literal loopback are exempt
from the new burst cap; wallet/live clients exempt literal private/loopback IPs and
localhost. Existing pacing and network enforcement still apply. A self-hosted DNS
endpoint used by wallet/live conservatively receives external pacing.

## Operator validation and activation

No tests, builds, RPC probes or services were run for this change. From the reviewed
checkout, with the existing native Cargo.lock and Nix dependencies available:

```bash
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_rpc_burst_pacing.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_data_compliance.py
nix develop .#native-rpc --command python -m unittest discover -s tests -p test_block_batch.py
nix build --no-write-lock-file .#native-rpc-app
```

Stop owned workers with Ctrl+C, or stop their existing systemd units before
changing a shared package. Retain the complete state/config/model directories and
the provider-limits ledger. Resume the existing paper session (no initialization):

```bash
nix run --no-write-lock-file .#paper
```

For live deployments, follow the existing manually authorized service workflow in
[production acceptance](production_acceptance.md#manual-live-integration-after-acceptance).
This change does not authorize a live start. Verify visible cooldowns expire,
checkpoint progress resumes, Retry-After is honored and freshness blockers remain
explicit. Slower pacing can worsen lag; it does not create complete history.

For rollback, stop owned workers first, rebuild the previous reviewed revision,
and resume through the same existing workflow. Preserve all journals and the new
pacing table; older code ignores that additive table. Existing provider denial and
Retry-After records remain in the unchanged `providers` table. Never delete the
ledger or clear an unresolved order to restart.
