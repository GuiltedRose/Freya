# Freya — Solana trading bot

<p align="center">
  <img src="Assets/Freya.png" alt="Freya project artwork" width="420">
</p>

> **Disclaimer:** This is a vibe-coded project built with AI. Its code and results
> may contain errors. Nothing here guarantees that you will make money; you can
> lose money, including your entire trading balance. This project is not financial
> advice. You are responsible for reviewing, testing, and choosing whether to run it.

A Python toolkit for researching and trading Solana spot markets. It combines
persistent paper trading, public market and wallet observation, strategy
research, neural entry/exit models, and configurable SVM, LSTM and historical
retrieval decisions with optional live execution.

The project is experimental. Backtests, modeled paper returns, and small live
samples do not establish profitability.

Current acceptance status and remaining market-feed blockers are recorded in
[ingestion acceptance](docs/ingestion_repair_20261102.md). Passing tests or starting
the dashboard does not establish that current on-chain event layouts are supported.

Live execution does not require paper history or promotion milestones. Start with
paper trading to collect data and assess behavior before risking real funds; this
is a recommendation, not an activation requirement. Live still requires explicit
`--execute` and wallet authorization. Smaller percentage sizing applies during
initial learning, followed by evidence-supported fractional Kelly sizing. Kelly
limits allocation; it does not establish a trading edge or prevent all losses.
Capital limits, source checks,
TP/SL, emergency stops and order reconciliation remain enforced. See
[live operation](docs/coordinated_pipeline.md#shared-services-and-live-wiring).

For the native/on-chain pipeline repair, operator-only build/test commands,
activation gates and rollback, see [pipeline recovery](docs/pipeline_recovery.md).
Pump web collectors remain disabled; paper collection requires a permitted feed
or an explicitly selected local index.

Momentum reclaim is the sole enabled entry strategy. New-launch sniping has been
removed. See [production acceptance](docs/production_acceptance.md) for active model
decisions, the aggregate test command and manually authorized live service wiring.
See [retrieval and Kelly](docs/retrieval_kelly.md) for net-expectancy validation,
hard allocation ceilings and independently earned concurrency tiers.
The [self-hosted RPC module](docs/selfhost_rpc.md) places a non-voting node inside
the existing capped network environment; it remains unbuilt and manually activated.

## What it does

| Area | Entry points | Purpose |
| --- | --- | --- |
| Paper trading | `run_paper.py`, `run_paper_compare.py` | Simulates trades with virtual balances, modeled costs, persistent state, and strategy comparisons. |
| Meme trading | `run_meme_live.py` | Evaluates Pump momentum/pullback/reclaim setups. Defaults to unsigned simulation; `--execute` enables real transactions. |
| Observation | `pump_market.py`, `wallet_watch.py`, `wallet_signals.py` | Collects market data and public activity from saved wallets. Observations provide research context rather than copying positions. |
| Research and learning | `meme_learning.py`, `neural_network.py`, `exit_evaluation.py`, `neural_autonomy.py` | Trains neural weights and evaluates frozen entry/exit candidates; qualification depends on the selected mode. |
| Manual-wallet research | `run_manual_research.py` | Archives wallet receipts separately from bot performance. |

The project now focuses on meme-coin momentum/pullback setups and neural exit
research. The earlier hourly portfolio, breakout/EMA/RSI, VWAP/range/squeeze,
short-strategy and atomic-arbitrage experiments have been removed. Historical
outcome journals remain intact for research and reconciliation.

## Learning and autonomy

The neural network learns from eligible completed paper trades, confirmed bot
trades, and receipt-backed outcomes from configured saved wallets. Both wins
and losses contribute. Full-batch training gives the positive class twice the total weight of the
negative class, regardless of class counts; the selection threshold compensates for this bias.
Scores remain uncalibrated; online selection is experimental, not validated profitability. Completed paper outcomes remain available across balance
resets; refills, rent returns, voided fills, and synthetic trades are not profit
labels. Paper results remain modeled data, separate from confirmed live returns.

Paper training refreshes between scans, including while other paper positions
are open. Live loads saved weights for inference and retrains only while flat inside its
one-hour consecutive-loss cooldown. Paper saves its model to
`learning.json` in its state directory; the meme runner rebuilds shared weights
from the outcome ledgers. Its default paper source is `state/paper_volume/paper.sqlite`.

Paper and live default to active learning decisions. Live retains its capital-tier
position limits, cooldown, sizing, safety and
execution checks; neural scores never bypass them. No live process is started
by training alone.

Paper starts at one position and a one-hour consecutive-loss cooldown, with no
random exploration overriding neural vetoes. Allocation and concurrency unlock
independently under the versioned capital policy; its configured capacity ceiling
is 20, subject to existing exposure, safety and freshness limits. Paper fills remain
hypothetical and cannot establish a live edge.

Live defaults to `--neural-mode learning` without requiring prior paper history
or promotion milestones. An empty model configuration permits initial learning;
configured unified models still apply their score vetoes. Operators can choose
`--neural-mode qualified` for prospective model qualification. Source permissions,
market freshness and execution checks remain mandatory.
SVM, LSTM and historical retrieval (RAG) are active entry vetoes only when their
unified database and reviewed candidate IDs are configured. Enabling unified
decisions requires all three families; missing required evidence blocks entries.
Retrieval uses eligible historical outcomes and cost-adjusted expectancy. It does
not generate trading advice through a language model. See
[model decision roles](docs/production_acceptance.md#components-and-actual-decisions).

See the [qualification policy](MEME_TRADER.md#autonomous-neural-qualification)
and [network review](research/neural_network_review.md).

Chart inputs now include completed-candle returns, drawdown from the recent
peak, volume change, and bearish-candle frequency. A separate exit network
learns one-minute changes in recorded net liquidation quotes while positions
are held. After sufficient training data and a successful position-grouped out-of-sample
comparison, its champion may suggest an earlier exit. Existing stops and time
limits take priority; live early-profit exits require positive minimum net
proceeds. Missing or stale chart data disables this optional signal.
These quote-based labels show associations, not causes or guaranteed fills.

The network uses structured market, saved-wallet, chart, position-return,
execution-cost and available on-chain concentration inputs. Missing values have
explicit indicators. Live entry inference runs after on-chain checks and swap
simulation, so those results participate in its decision. Paper uses its modeled
costs and leaves unavailable chain observations unknown. It does not treat raw
provider claims or unavailable data as verified evidence.

Exit evaluation freezes a challenger and tests it on chronologically later,
whole positions; overlapping train/test positions are purged. Only positions
with a known rule-based close support the exit-outcome comparison. The console
shows position-weighted accuracy, Brier score, estimated net improvement versus
rules, champion status and rollback counts. These are out-of-sample quote-replay
metrics, not actual counterfactual fills or proof of live profitability.

Exit champions require positive improvement with no worse replay drawdown or
prediction metrics. Later deterioration rolls back to the previous champion,
or to rule-based exits. All valid historical samples stay in the ledgers;
consumed evaluation blocks can train later challengers, which need fresh tests.
Evaluation state is saved in `exit_evaluation.json` with a transactional
`exit_evaluation.checkpoint.sqlite` recovery checkpoint. Challenger IDs, frozen
weights and partial evaluation counts survive process restarts in both runners.
Console output distinguishes current progress from completed challengers and
cumulative evaluated positions. A completed rejection starts a new challenger;
it does not discard the previous result. Interrupted exports recover from the
durable checkpoint. Missing observations or incompatible feature versions pause
evaluation with an explicit reason; a restart alone never rejects a challenger.
Saved position scores are immutable. Back up the whole state directory.
Unchanged data is cached.

Both paper and live summaries show `LIFETIME` totals alongside `LAST 20`: valid completed trades,
net P&L, win rate, and wins/losses/break-even counts. A partial window shows its
actual count. Paper spans archived runs without counting virtual refills;
invalid/synthetic outcomes and open positions are excluded. Live also subtracts
recorded failed-order fees since the earliest included entry. This recent window
can span different models and is separate from lifetime training and exit OOS
metrics. Learning parameters and risk limits are unchanged.

**Data-source audit:** Pump-host HTTP collectors are currently disabled: their
web access was not verified as permitted, and the official Developer API terms
prohibit AI training/evaluation. Existing histories are retained pending rights
review. The replacement uses finalized native Solana RPC with source permissions
and shared rate limits. The optional qualified live mode retains its ten-consecutive-
promotion gate; ingestion does not qualify models or authorize live orders.
Retired web polling wrappers are removed; saved-data parsers and shared native
paper-cost helpers remain. Historical comparisons require `--feed` or `--replay-db`.
See [audit evidence and source inventory](research/learning_source_audit_2026-10-30.md).

For Nix development, separate server services and current throughput limits, see
the [on-chain deployment guide](docs/onchain_deployment.md).

An opt-in [local on-chain indexer](research/onchain_indexer.md) now provides
durable finalized-block ingestion and shared paper/live adapters. The
[ingestion repair](docs/ingestion_repair_20261102.md) pins the published SDK event
contract, including quote transfer fees and multi-hop receipts. Current replay and
mainnet samples decode correctly. Sustained public-RPC capacity and complete real
market windows remain separate acceptance requirements; missing evidence still
blocks entries and learning eligibility.

## Getting started

Use Python 3.10 or later. Nix commands below require Nix with flakes enabled.
Examples using `--reference-lock-file ./flake.lock` assume an existing reviewed local
lock; if none exists, omit that option (the examples still prohibit lock writes).
From the repository directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Paper trading

The coordinated flake runner now starts the RPC indexer, periodic learner and
paper consumer together. It reads public collection settings from `.env` while
keeping wallet signing fields out of collection/learning processes. A public RPC
URL selects built-in, question-free setup for official public mainnet and self-hosted
nodes. Setup checks mainnet identity, deployments and the oracle at runtime:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --configure
# Optional: startup also performs built-in setup automatically when needed.
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --preflight
```

Read [coordinated startup](docs/coordinated_pipeline.md) for the exact configuration
requirements, source throughput/warmup, safe shutdown and shared paper/live operation.
Setup backs up existing configurations before replacement. Preflight
lists blockers together and exits before opening paper state or connecting to RPC.
Paper starts with the existing percentage budget reduced by its allocation tier
(initially 25% of that budget), then uses supported Kelly sizing as evidence grows.
Zero means no trade; live uses the same initial percentage-sizing approach without
requiring paper outcomes. Qualification is optional; execution risk limits remain.

For a **new** paper session using the current meme rules:

With Nix, after configuration passes preflight:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#paper -- --initialize
```

This explicitly creates a new virtual session with 0 USDC, 0.1 SOL and an
illustrative initial valuation of $100/SOL. Override those values with `--balance`,
`--native-sol` and `--sol-price`. Initialization refuses an existing paper database.
Resume with `nix run .#paper`; it loads the saved inventory without resetting it.

Normal paper startup, including resume, automatically prepares missing configs or
generated unapproved templates for built-in profiles, with no questions and no terminal
required. `SOLANA_RPC_URL` selects official public mainnet, literal loopback or private
node addresses. For your own node at a DNS hostname set `CRYPTO_BOT_RPC_MODE=selfhosted`.
Node namespace/bandwidth enforcement remains mandatory; public mainnet uses the
remote-provider path. Custom third-party providers retain explicit setup.
These are operational defaults, not a certification of provider rights, model quality
or IDL compatibility. Actual layout, deployment-change, oracle and source-data checks
remain active. Setup saves owner-only configuration files with backups.

Live has the same setup and supervision; paper history is optional:

```bash
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#live -- --configure
# When you choose to activate real-money trading:
nix --extra-experimental-features 'nix-command flakes' run \
  --reference-lock-file ./flake.lock --no-write-lock-file \
  .#live -- --execute
```

First-time `--execute` uses the same automatic built-in setup if configs are missing.
Setup alone never loads a signer or starts trading. Live execution requires a local
signing source; collection and paper workers do not receive it.

The runner defaults to `state/paper_volume` and `state/onchain/index.sqlite`; both
the direct paper CLI and coordinated runner use the same paper-directory selection.
If only the older `state/paper_memes/paper.sqlite` exists, they reuse it without
moving data. If both paper directories contain sessions, select `--state-dir`
explicitly; sessions are never merged or reset automatically. Both
paths can be overridden after `--`. The indexer bootstraps only an empty database
at a finalized head, then resumes its durable checkpoint. It discovers markets
every 30 seconds. Use
`--no-loop` for one paper scan (the supervised data workers may collect multiple
cycles), or `--status` for the saved pipeline report without starting workers.
`nix run .#paper-raw -- ...` retains the direct CLI, including feed-replay mode.
The runner starts the data workers but never approves data sources. Keep `flake.lock`
ignored; runner checks use `--no-write-lock-file` to preserve any local lock.
To use an existing ignored local lock reproducibly, also pass
`--reference-lock-file ./flake.lock` before `.#paper`. Keep that local file;
there is no need to remove it from `.gitignore` or add it to Git.

Without Nix, use `.venv/bin/python run_bot_pipeline.py --mode paper --initialize`
for the same supervision after installing dependencies and configuring the selected
backend; a Rust backend also requires the native extension. Omit `--initialize`
to resume. The direct paper CLI below remains available for deployments
that already run a separately configured indexer:

```bash
.venv/bin/python run_paper.py \
  --state-dir state/paper_volume \
  --onchain-db state/onchain/index.sqlite \
  --rules meme_live_rules.json \
  --balance 0 --native-sol 0.1 --sol-price 100 \
  --watchlist paper_watchlist.json --discover --loop --interval 30
```

These are virtual funds. The example starts with 0.1 SOL valued at an illustrative
$100 per SOL; adjust the inventory and initial valuation for your experiment.
Review `paper_watchlist.json` to choose the public wallets to observe. Run the
new-session command only for an unused state directory; the resume command
keeps existing balances and rules. Stop a running loop with `Ctrl+C`.

Resume an existing session with its saved inventory and rules:

```bash
.venv/bin/python run_paper.py --state-dir state/paper_volume \
  --onchain-db state/onchain/index.sqlite \
  --watchlist paper_watchlist.json --discover --loop --interval 30
```

Inspect the same session without starting its collection loop:

```bash
.venv/bin/python run_paper.py --state-dir state/paper_volume --status
```

Export outcome summaries and neural weights:

```bash
.venv/bin/python meme_learning.py --paper-db state/paper_volume/paper.sqlite
```

Paper CLI defaults are `--max-positions 1 --loss-cooldown-seconds 3600`.
Production runs also apply `capital_policy.example.json`: capacity starts at one
and grows only through independently earned concurrency tiers. The CLI accepts
1–20 positions, but changing that option alone does not unlock capital-policy tiers.
Paper options never change live limits. See [capital policy](docs/retrieval_kelly.md).
The console shows trades, equity, positions, training counts and current blockers.
Use `--json` for full console reports. Full recorded observations and network
inputs remain in the journals and reports; display formatting does not filter
training data.

Reports and journals stay in the selected state directory. Capital-managed
production runs do **not** automatically refill exhausted or drawdown-halted paper
balances. Archived reset outcomes remain available subject to current eligibility
rules; virtual funding is never trading profit. See the
[paper guide](MEME_TRADER.md#persistent-paper-runner-restart-and-reset) for explicit
resets, cost models and rule changes. Back up the entire state directory before
operator changes; never overwrite current journals with an older backup.

### Unsigned simulation

Set your public address, then observe and simulate the meme runner:

```bash
BOT_PUBLIC_WALLET="REPLACE_WITH_YOUR_PUBLIC_SOLANA_ADDRESS"
.venv/bin/python run_meme_live.py --wallet "$BOT_PUBLIC_WALLET" --loop
```

Replace the placeholder with your public address. This mode reads network data
and simulates transactions without submitting them. It needs no private key or
`.env` file and does not maintain a virtual paper account.

### Live execution

Review [MEME_TRADER.md](MEME_TRADER.md) and [.env.example](.env.example) before
using real funds. Create an owner-only `.env` from the example if one does not
already exist. Configure a permitted `SOLANA_RPC_URL` (HTTPS for remote providers; explicit
loopback/self-hosted settings follow the deployment guide) and exactly one signing source:
`SOLANA_KEYPAIR_PATH` or `SOLANA_PRIVATE_KEY`.

The example includes the public mainnet RPC URL, which is safe to publish.
Wallet secrets and provider URLs containing API keys must remain private.

Create `.env` without overwriting an existing configuration, then edit it locally:

```bash
if [ ! -e .env ]; then cp .env.example .env; fi
chmod 600 .env
```

To explicitly enable real meme trades:

```bash
.venv/bin/python run_meme_live.py --execute --loop
```

Use one live runner per wallet. Exit monitoring requires the process and network
to remain available; stopping the runner does not liquidate an open position.
Preserve `state/` when orders or positions need reconciliation. To check an
existing pending meme order without scanning for or submitting a new order:

```bash
.venv/bin/python run_meme_live.py --execute --reconcile-only
```

Keep private keys, recovery phrases, `.env`, and live state out of source control,
chat, logs, and issue reports. Live execution can lose funds.

## Documentation

- [Meme trader guide](MEME_TRADER.md): configuration, execution scope, paper workflows, and learning policy.
- [Meme strategy review](research/meme_strategy_review.md): strategy research and limitations.
- [Neural-network review](research/neural_network_review.md): architecture, adaptation, and verification.
- [Production acceptance](docs/production_acceptance.md): strategy, checks and operating requirements.

## Nix development and isolated deployment checks

`nix develop` supplies Python, Rust (rustc, Cargo, rustfmt, Clippy), and project dependencies. On Linux it also supplies
nftables, iproute2, ping and process utilities; no global package installation is
needed. No services start when entering the shell.

Run the isolated NixOS lifecycle test on the monolith:

```bash
nix build .#checks.x86_64-linux.solana-network-lifecycle
```

This uses the flake's pinned nixpkgs and local VM fixtures, not Solana mainnet.
The new VM coverage has not been run by the coding agent. See
[network deployment and verification](docs/nixos_network_lifecycle.md) for module
options, opt-in activation, persistent accounting and safe shutdown. Host/router
controls remain necessary for household priority and ISP-level accounting.

## Development and support

Run validation manually on the operator host. The Python-only suite is:

```bash
.venv/bin/python -m unittest discover -s tests
```

For native packaging and the protected aggregate validation command, use
[production acceptance](docs/production_acceptance.md#run-everything-together-on-the-monolith).
For service prerequisites, activation, monitoring, recovery and rollback, follow
[pipeline recovery](docs/pipeline_recovery.md) and
[self-hosted RPC](docs/selfhost_rpc.md). Stop live services before rollback and
retain all newer orders, outcomes, checkpoints and unresolved signatures.

Fresh historical synchronization starts at slot 0 unless an explicit override is
supplied; existing valid progress resumes. See the
[Genesis audit and validation](docs/genesis_sync_audit.md) and
[explicit recovery after runtime data loss](docs/native_fresh_start.md).

Use focused tests for behavior changes. Review changes to execution limits and
preserve outcome journals when investigating failures.

Maintained by [GuiltedRose](https://github.com/GuiltedRose). Report bugs and
request features through [GitHub Issues](https://github.com/GuiltedRose/crypto-bot/issues),
without credentials or private state.

Licensed under the [GNU Affero General Public License v3](LICENSE).

The [Rust RPC transport](docs/rust_rpc_engine.md) provides bounded HTTP/WebSocket
transport and generic Borsh extraction. Python retains source/deployment admission,
normalization, storage, checkpoints, candles and learning. Explicit Rust selection
requires the native extension; it does not silently substitute Python. Default
packaging and coordinated native-RPC apps have different backend requirements.
See [ingestion acceptance](docs/ingestion_repair_20261102.md) for recorded build and
receipt-replay results and remaining runtime checks. Those results do not establish
sustained feed capacity, a trading edge, or acceptance of a self-hosted node.
[Binary decoder details](docs/native_decoder.md) cover the separate binary adapter.

External RPC pacing is shared across paper/live workers: see
[burst cooldowns and operator rollout](docs/rpc_burst_pacing.md).

## Paper challenger operation

Independent management experiments, bounded replay, evidence requirements and
operator-only validation/rollback commands: [challenger operations](docs/challenger_operations.md).
