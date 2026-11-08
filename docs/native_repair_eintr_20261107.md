# November 7 EINTR shutdown correction

## Confirmed failure and fix

The operator reports controller deactivation followed by a systemd peer stop;
`recv_from` returned errno 4 with `stop_observed=true` and the saved lease still
unexpired. The previous receive match classified `Interrupted` as a socket failure,
returned an error and ultimately published `state=blocked` with exit 1.

`native/freya_node/src/follow.rs::receive_error` now distinguishes:

- Interrupted + stop requested: break the receive loop normally. Existing
  `repair_wait_result` records `stop_requested`; no timeout or socket error is added.
- Interrupted without stop: retry only before the **existing** receive deadline.
  An expired receive window ends the wait, without creating another deadline.
- WouldBlock/TimedOut: retain existing retry behavior under the loop deadline.
- Every other error: retain the original error and errno, including during stop.

The loop still checks the same lease and traffic counters before subsequent reads.
On the graceful EINTR path it performs no further receive/send, backoff exits on
the stop flag, the outer session returns success, and successful gossip teardown
leads to `state=stopped` rather than `blocked`. Genuine authorization, persistence,
gossip-join or socket failures can still produce `blocked`; they are not hidden.
No budget, receive window, lease, pacing, endpoint or signature rule changed.

## Verification and limits

Executed 11 exact production-helper tests in an isolated offline Rust harness,
including three new EINTR regressions for shutdown, retry/expired deadline and
preserving actual socket errors. Executed
`python -m unittest tests.test_solana_network_lifecycle`: 8 passed, OS calls mocked.
`git diff --check` passed.

Attempted offline native-peer/native-node builds: Nix daemon socket returned
`Connection refused`. Complete engine compilation and a real systemd shutdown
are therefore unverified here. No service/network session was started. The
regressions exercise the reported classification and normal wait-completion path;
they are not evidence of a newly successful Monolith invocation or valid shred.

Operator commands with Nix available:

```bash
nix build --offline --no-write-lock-file .#native-peer .#native-node
```

The peer build runs the complete Rust engine fixtures including these tests.
Refresh the deployed immutable source/package references only through the existing
[review/backup/build/activation and rollback procedure](native_network_investigation.md).
The follow implementation resides in the pinned peer engine, not only the launcher.
A subsequent explicitly authorized bounded session should confirm a stop during
receive records `stop_requested`, no `socket_receive` failure and a clean exit,
provided no independent failure occurs. Do not automatically start that session.

## Separate controller finding: 28.038-second lifetime

The cause is **not determined from the supplied evidence**. Clean controller
shutdown does not prove normal 55-second expiry. Source inspection of
`solana_network_lifecycle.py::start` confirms normal return can follow lease/cycle
expiry, sampled `rx+tx >= grant.bytes`, or SIGTERM/SIGINT. Setup and READY consume
time within the same lease. A signal's sender is not identified by the controller
itself. The reported unexpired follower snapshot argues against simply assuming
nominal lease expiry; correlate the actual controller expiry and grant instead.
No controller timing or accounting changes were made for this investigation.

Read existing records on Monolith, without starting services. Confirm the ledger
path against the installed controller policy's `ledger` first; these commands use
the repository default. Keep raw configuration/journal output private.

```bash
sudo systemctl show solana-budget-network.service freya-peer-discovery.service \
  -p InvocationID -p Result -p ExecMainCode -p ExecMainStatus -p ExecStart \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p ActiveEnterTimestamp
sudo jq -c 'select(.phase=="session_stopped_gap") |
  {time,grant,expires,ready_at,stop_reason,stop_signal}' \
  /var/lib/solana-network/network-events.jsonl | tail -n 3
sudo sqlite3 -readonly /var/lib/solana-network/budget.sqlite \
  'SELECT id,created,bytes,rx,tx,rx+tx AS observed_total FROM grants ORDER BY created DESC LIMIT 5;'
# Replace placeholders with the affected invocation's host-local time interval.
sudo journalctl -u solana-budget-network.service -u freya-peer-discovery.service \
  --since 'YYYY-MM-DD HH:MM:SS' --until 'YYYY-MM-DD HH:MM:SS' \
  -o short-precise --no-pager
```

Match the stopped event's grant ID to its ledger row, and match event/journal
timestamps and invocation IDs. `grant_exhausted` plus matching counters explains a
quota stop; `stop_requested` identifies a received signal but still needs journal
context for its origin; `lease_expired` must agree with recorded expiry.
Do not compare a later grant or cumulative namespace traffic to this invocation.
If these records are absent or old fields missing, report the evidence gap rather
than reset the ledger or increase its allowance.
