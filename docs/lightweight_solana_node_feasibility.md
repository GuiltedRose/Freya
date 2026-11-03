# Freya lightweight Solana node feasibility

Research date: November 3, 2026. **Research only; no implementation or deployment.**

The deployment is a private node component bundled with Freya and run on the
operator's own machine. It does not serve public RPC customers or multiple users.
The objective is the smallest reliable syncing/running footprint, including Freya,
within Monolith's **64 GiB total installed RAM**. Public-service capacity and a GUI
are not requirements. Remote administration is optional.

Evidence labels: **Source** = inspected code/official documentation; **Observed** =
operator-supplied results already recorded in this repository; **Estimate** = an
unmeasured engineering allocation; **Hypothesis** = requires a future experiment.
None of the estimates below are demonstrated node performance or production acceptance.

## 1. Executive summary and feasibility verdict

**There is no demonstrated full independently validating mainnet node configuration
in this study that fits beside Freya on Monolith and keeps up continuously.** This
is not a proof that a new disk-heavy implementation cannot do it. The minimum
achievable RAM for that implementation remains unknown.

The credible own-project direction is a specialized, private data plane with an
explicit verification boundary: reuse established chain ingestion/verification
machinery; investigate a disk-backed global account index and bounded execution
working set; expose only Freya's internal needs. Do not write a new consensus/SVM
implementation before testing storage, replay speed and ingress feasibility.

Removing public RPC, optional secondary indexes and long general-purpose history
helps, but does not remove global replay dependencies. A small filtered collector
could plausibly target **4–8 GiB of component memory**, but that is an experimental
budget, not evidence of a 4–8 GiB independently validating node.

Two independent blockers precede memory tuning:

* The existing managed-node policy uses approximately 16 kB/s per direction and a
  finite, manually started session of about 55 seconds. Unchanged, that policy
  cannot support uninterrupted peer-node synchronization. No policy change is
  proposed or performed here.
* The dual E5-2630 v4 system has older, low-clocked physical cores. Forty logical
  threads are not forty physical cores or proof of replay capacity.

Agave's published baseline is 256 GB RAM; Firedancer's current guide lists 256 GB
minimum and 512 GB recommended. These are upstream recommendations/requirements,
not mathematical minimums for every possible custom verifier. [S1](https://docs.anza.xyz/operations/requirements),
[S2](https://docs.firedancer.io/guide/getting-started.html).

## 2. Freya requirements and Monolith hardware

**Operator-supplied hardware:** two Xeon E5-2630 v4 CPUs; 40 logical threads;
64 GiB installed RAM; Linux/NixOS; limited storage and strict network controls.
Current usable RAM, DIMM layout, NUMA placement, NVMe topology, disk endurance,
actual spare capacity and physical uplink capacity were not measured.

Intel lists each processor as ten physical cores/twenty threads, 2.20 GHz base,
up to 3.10 GHz turbo, with AVX2. Turbo is not a sustained all-core guarantee. The
specification does not establish the SHA/AVX512 acceleration expected by modern
validator guidance; future profiling must verify actual CPU flags and the selected
binary's instruction requirements. [S3](https://www.intel.com/content/www/us/en/products/sku/92981/intel-xeon-processor-e52630-v4-25m-cache-2-20-ghz/specifications.html).

Required data properties, independent of transport:

| Requirement | Minimum retained information/behavior |
| --- | --- |
| Pump/PumpSwap receipts | Exact program/version, mint/pool identity, parent/CPI routes, transaction and instruction ordering, logs, errors, relevant account/balance/token-extension evidence |
| Complete market windows | Explicit slot coverage, skipped-slot evidence, fork/root status, deduplication and gap detection; absence of a notification is not proof of no trade |
| Candles and execution context | Causal, correctly ordered prices/volumes and genuine costs; preserve quote-token fees and multi-hop attribution |
| USD values | Historical oracle observations available at the event time, source identity, confidence/freshness policy and slot bounds |
| Recovery | Durable original evidence, checkpoint/manifest, content hashes, source/schema versions and replayable provenance |
| Learning | Genuine eligible completed outcomes; node output must not manufacture labels, buyer identities or model qualification |

Local source anchors: `onchain_decode.py`, `onchain_routes.py`,
`onchain_store.py:coverage/conversion`, `onchain_market.py`,
`solana_binary_ingest.py`, `unified_learning.py`. The latter binary adapter
explicitly separates parsed input from consensus/execution admission. Existing
market checks require fresh index/swaps and complete historical coverage.

The operator's successful ten-call mainnet probe established transport and decoder
operation on two blocks at one moment. It measured neither a locally replaying
node nor whole-system RAM. Local loopback RSS includes fixture-server overhead;
it cannot be used as a standalone native-client memory measurement. See
[running investigation](rpc_performance_changes.md).

## 3. Agave source analysis

Source inspected at Freya's existing Agave pin
`825efd18292aff6ffcf9daa0f7612f21b3531a72`, rather than assuming current master is
the deployed mainnet version. Reads were bounded source excerpts, not a complete
audit or execution of the client.

| Inspected upstream path | Relevance |
| --- | --- |
| [`core/src/tvu.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/core/src/tvu.rs) | Receive/replay assembly connects replay, consensus and block metadata |
| [`core/src/replay_stage.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/core/src/replay_stage.rs) | Transaction replay, verification workers, forks and root processing; essential verification work survives removal of serving APIs |
| [`core/src/commitment_service.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/core/src/commitment_service.rs) | Stake-aware root aggregation and separate consensus integration paths; commitment is not an HTTP formatting property |
| [`gossip/src/gossip_service.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/gossip/src/gossip_service.rs) | Peer discovery/control plane, not a substitute for complete ledger delivery |
| [`ledger/src/shred.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/ledger/src/shred.rs) | Data/coding shreds and erasure recovery; peer delivery needs authenticated reconstruction and repair |
| [`accounts-db/src/accounts_db.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/accounts-db/src/accounts_db.rs) | Persistent account storage and caches; disk-backed data is already part of the architecture |
| [`accounts-db/src/accounts_index.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/accounts-db/src/accounts_index.rs) | Explicit in-memory-only versus threshold/eviction index configuration, storage drives and flush threads |
| [`accounts-db/src/accounts_db/accounts_db_config.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/accounts-db/src/accounts_db/accounts_db_config.rs) | Read-cache watermarks, write-cache bounds and index configuration are genuine tuning surfaces |
| [`accounts-db/src/append_vec.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/accounts-db/src/append_vec.rs) | Persistent account files and bounded file I/O; changing mapping strategy trades RAM pressure against I/O |
| [`runtime/src/snapshot_bank_utils.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/runtime/src/snapshot_bank_utils.rs) | Snapshot verification/unarchive, storage reconstruction and bank/epoch state recovery |
| [`geyser-plugin-interface/src/geyser_plugin_interface.rs`](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/geyser-plugin-interface/src/geyser_plugin_interface.rs) | Exported account/transaction metadata is downstream of the runtime |

**Inference:** disk index/cache tuning deserves study before a new implementation.
However, an index eviction threshold is not a whole-node memory cap. Fork banks,
execution/program caches, network queues, snapshot reconstruction, allocator
retention and filesystem pages remain. Replacing RAM lookups with SSD lookups must
be evaluated on the replay critical path, including cold recovery.

Non-voting operation is legitimate: Anza documents that an RPC node normally does
not vote and runs the validator process without a vote account. A private endpoint
or removal of full RPC methods does not remove replay/state costs. [S4](https://docs.anza.xyz/operations/setup-an-rpc-node).

## 4. Firedancer source analysis

Inspected release tag: **v26.10.5**. This is a source reference, not confirmation of
Monolith compatibility or of the exact feature set active on mainnet.

* [`src/app/firedancer/topology.c`](https://github.com/firedancer-io/firedancer/blob/v26.10.5/src/app/firedancer/topology.c)
  connects networking, repair, replay, execution, state and consensus tiles.
* [`src/discof/replay/fd_replay_tile.c`](https://github.com/firedancer-io/firedancer/blob/v26.10.5/src/discof/replay/fd_replay_tile.c)
  describes reconstruction from shreds/FEC sets, repair of missing data and delivery
  into replay. Receiving shreds alone does not supply executed receipts.
* [`src/flamenco/runtime/fd_runtime.c`](https://github.com/firedancer-io/firedancer/blob/v26.10.5/src/flamenco/runtime/fd_runtime.c)
  depends on execution, account lookup, sysvars, stake and program-cache machinery.
* [`src/flamenco/accdb/fd_accdb.h`](https://github.com/firedancer-io/firedancer/blob/v26.10.5/src/flamenco/accdb/fd_accdb.h)
  exposes fork-aware account access and snapshot-load coordination.
* [`src/vinyl/fd_vinyl.h`](https://github.com/firedancer-io/firedancer/blob/v26.10.5/src/vinyl/fd_vinyl.h)
  separates persistent storage, metadata, caches and asynchronous requests. The
  attempted `src/vinyl/README.md` lookup was absent at this tag; conclusions use the
  inspected headers, not that nonexistent document.

The strongest sizing evidence is the tagged
[`config/default.toml`](https://github.com/firedancer-io/firedancer/blob/v26.10.5/src/app/firedancer/config/default.toml#L700):
account data is hybrid RAM/disk, but its account index stays in memory. Its comments
estimate about **100 GiB per billion accounts**, with configured maximum
**1.3 billion accounts** and a **12 GiB hot-account cache**. That suggests roughly
130 GiB of index capacity before cache/runtime overhead; it is a sizing inference,
not a measurement of today's live account count. Arbitrarily lowering capacity can
make the client fail when the real population exceeds it. Smaller cache alone does
not solve this index allocation. The comments also reserve fork/program state.

Its getting-started guide also estimates around 32 GiB available memory for the
build itself; compiling concurrently with the modeled operating peak would exceed
the host budget. Any future build needs a separate scheduled window or build host.
Firedancer's CLI documents preallocated locked huge pages and a `mem` planning
command. Its displayed example is around 159 GiB locked, not a universal minimum.
Shared tile mappings must be counted once. Historical Frankendancer/hybrid results
must not be presented as full Firedancer results. [S5](https://docs.firedancer.io/api/cli.html),
[S6](https://docs.firedancer.io/guide/faq.html).

**Verdict:** useful source for storage/queue design, not an off-the-shelf sub-64 GiB
solution. Existing huge-page and kernel-bypass assumptions also need explicit NixOS
isolation review; do not grant broad capabilities or bypass accounting to make it run.

## 5. Solana networking and verification requirements

A private independently validating observer still needs:

1. An identified cluster/genesis and a justified starting state/checkpoint.
2. Peer discovery and reliable block/shred acquisition, including missing-data repair.
3. Producer/transaction authentication, erasure reconstruction and valid ordering.
4. Correct program execution with required accounts, sysvars, feature activations,
   lookup tables, fees and stake/epoch transitions.
5. Fork/root/finality validation for the **actual protocol version** being followed.
6. Durable recovery state and a trustworthy account of missing data.

Sending one's own votes is not required. Verifying others' consensus evidence and
stake transitions is a different responsibility. The pinned Agave source contains
both Tower-related and Alpenglow commitment integration; this study does not infer
mainnet activation from the presence of either code path.

| Verification level | What it establishes | What it does not establish |
| --- | --- | --- |
| TLS and reviewed source | Delivery from an approved endpoint | Truth of the endpoint's finalized/complete claims |
| Transaction/shred signatures and structure | Authentication of signed inputs/producer material, assuming correct keys/schedule | Execution success, truthful logs/balance changes, canonical finality or absence of omitted activity |
| Authenticated chain/finality proofs | Only the properties committed by the validated proof and trusted bootstrap | Arbitrary receipt/account metadata outside that commitment |
| Full local execution plus consensus verification | Execution/fork validation from the accepted starting state | Correctness of an unverified pre-bootstrap history or economic truth of an oracle price |

`getBlock` exposes transactions/metadata and commitment selection; `getAccountInfo`
returns state/context rather than a general authenticated inclusion proof. A block
hash or signed transaction is not a blanket signature over every JSON receipt
field. A light verifier must identify the precise commitment/proof for the field
it uses. [S7](https://solana.com/docs/rpc/http/getblock),
[S8](https://solana.com/docs/rpc/http/getaccountinfo).

A snapshot's local checksum proves file integrity, not who chose a truthful state.
Using a trusted checkpoint is a bootstrap trust assumption; it does not constitute
replay from genesis. Two agreeing upstreams also do not automatically establish
independent verification, especially if they share infrastructure.

## 6. Removable components and tradeoffs

| Proposed removal/change | Correctness, dependency and recovery consequence |
| --- | --- |
| Own voting/vote account | Removes vote submission/reward participation, not verification of consensus or stake state |
| Public/general RPC and RPC pubsub fan-out | Appropriate here: expose internal IPC or a tiny loopback interface. Retain replay, root processing and the required local queries/exporter |
| Optional program/mint/owner secondary indexes | Avoid global query indexes Freya never uses; retain whatever primary account lookup execution requires |
| Old general ledger history | Can reduce future node disk retention after a verified root/recovery horizon; requires a gap/backfill source beyond that horizon. Preserve existing Freya journals and original eligible evidence |
| Leader block production/transaction ingress | Candidate for a non-producing observer only after auditing topology dependencies. Do not remove repair/validation functionality because it shares code or ports |
| Cold account data in RAM | Move to disk with bounded cache; all required state remains available and authenticated. SSD misses/compaction can prevent catch-up |
| Accounts/transactions unrelated to Pump | Safe for downstream indexing, **not generally safe for full replay**. Shared token programs, account funding, CPIs and future dependencies can cross the selection boundary |
| Full replay in a hybrid client | Major resource saving, but upstream execution/receipt claims remain trusted unless independently proven |
| Stake/epoch state, signature checks, fork tracking or required repair | Not removable while claiming the same independent verification guarantees |
| Long decompression windows/unbounded buffers | Replace with bounded storage/streaming; overflow creates a visible gap and replay work, never a silent missing-trade interval |

Filtering must include nested/routed Pump activity, not just top-level program IDs.
For completeness, every finalized slot needs a manifest/count or an equivalent
validated commitment; a filtered transaction list cannot prove it contains all
matching transactions merely because every delivered item decodes.

## 7. Minimum-memory architecture analysis

**No defensible numeric minimum for full independent mainnet verification was
established.** The unavoidable state can in principle live mostly on disk; the
sustainable hot working set, lookup metadata, atomic execution unit and recovery
working set determine practical RAM. Those require measurements on this workload.
Disk capacity is not proof of sufficient random-I/O latency.

| Proposed node-component budget | Full independent node assessment | Smaller private data component |
| --- | --- | --- |
| 48 GiB | Experimental Agave storage/cache target only; current default Firedancer sizing does not fit. Leaves too little room for some whole-system peaks | More than a narrow collector should need, unless retaining unnecessary copies |
| 32 GiB | Most useful upper research gate with whole-system headroom; no sustained-mainnet evidence on these CPUs | Comfortable design envelope in principle; measure actual runtime |
| 16 GiB | High-risk disk-heavy verifier hypothesis; CPU/I/O and initialization may dominate | Plausible bounded collector/indexer target, unmeasured |
| 8 GiB | No verified full-replay configuration found | Worthwhile target for a specialized streaming data plane that retains explicit upstream trust |
| 4 GiB | Stretch research hypothesis, not a deployable full-node claim | Possible experiment for bounded filtering/archive; schema/state/decoder peaks still need proof |

These are component budgets, not permissions to allocate all RAM to the node.
At every size, full replay must preserve required state access and consensus rules.
An OOM cap that stops the node is containment, not evidence it fits.

Priority optimization candidates:

* Disk-backed **primary** index plus byte-bounded hot caches, measured under cold
  lookup/recovery. Avoid duplicating a full RAM directory over a disk store.
* Stream snapshot/archive records to final storage, limiting concurrent decode,
  index construction and compaction. Do not retain full decompressed snapshots in RAM.
* Bound queues by bytes as well as item counts; spill authenticated material to a
  durable journal. On overflow, stop admission and mark/recover the gap.
* Preserve compact binary data; eliminate JSON/base64 round trips and duplicate
  process caches where ownership permits. Borrowing buffers must respect lifetimes.
* Track fork state explicitly. Reducing fork capacity is not a safe shortcut unless
  the supported protocol's worst-case behavior is still covered.
* Reclaim caches by observed value; do not call mmap'd pages free memory. A smaller
  cache can turn RAM savings into replay stalls and a growing backlog.

Agave already implements several of these ideas. A custom Rust rewrite does not
inherit a memory advantage simply because it is new or written in Rust.

## 8. Compression and processing optimization findings

### Published measurements, not Freya measurements

The Zstandard repository publishes this **Silesia corpus** comparison on an
**i7-9700K at 4.9 GHz**, Ubuntu 24.04, lzbench/GCC 14.2. It is a useful codec
baseline, not Solana data or a forecast for Monolith. [S9](https://github.com/meta/zstd#benchmarks).

| Codec | Raw/compressed ratio | Compression MB/s | Decompression MB/s | Peak RAM / Freya chunk latency |
| --- | ---: | ---: | ---: | --- |
| Zstd 1.5.7, level 1 | 2.896 | 510 | 1,550 | Not reported for this workload; unknown |
| Zstd 1.5.7, fast 1 | 2.439 | 545 | 1,850 | Unknown |
| LZ4 1.10.0 | 2.101 | 675 | 3,850 | Unknown |
| zlib 1.3.1, level 1 | 2.743 | 105 | 390 | Unknown |

No Solana-specific ratio, peak memory, CPU time or decompression latency was
measured here. Future CPU time is input bytes divided by **measured local**
throughput, plus framing/I/O costs; those published rates cannot be scaled by core
count or clock ratio to certify this server.

| Technique | Useful role | Integrity/resource consequence |
| --- | --- | --- |
| Zstd streaming | Cold evidence/archive and versioned dictionaries | Window size bounds decoder history, not total process RSS; cap frame/window/output sizes and preserve dictionary identity |
| LZ4/frame streaming | Hot spool or replay where CPU latency dominates | Lower ratio may increase disk/network usage; its dictionary history is limited, not a replacement for global state |
| Dictionary compression | Repeated schema/address/field patterns in small records | Train on allowed, separate samples; store dictionary/version/hash with archives and retain it for recovery |
| Delta/varint encoding | Slot/time/amount columns where exact reversible encoding exists | Preserve nulls, negative deltas and integer precision; no invented timestamps or rounded prices |
| Column-oriented layouts | Efficient analytical scans over retained market observations | Keep receipt/order/provenance linkage; column projection alone is not complete replay evidence |
| Domain-specific packing | Raw 32-byte public keys and repeated IDs rather than textual duplication | Dictionary/index memory must be counted; never drop required token extensions, routes, fees or metadata |
| Custom compressor | Only after established codecs and representation changes are benchmarked | New corruption/security/maintenance burden; no evidence yet justifies one |

Base64 uses roughly four bytes for three binary bytes: about 33% encoding overhead;
removing it saves about 25% of that encoded portion, not 33% of an entire response.
Signatures and hashes contain high-entropy material, so extreme universal compression
ratios are not credible. Compression after download cannot reduce that download's
ISP traffic. TLS/VPN ciphertext is not a useful downstream compression target.

Zstd's window is an explicit memory/compatibility choice, and library workspace,
workers and dictionaries add overhead. LZ4 supports bounded streaming/dictionaries.
[S10](https://facebook.github.io/zstd/zstd_manual),
[S11](https://github.com/lz4/lz4/blob/dev/examples/streaming_api_basics.md),
[S12](https://datatracker.ietf.org/doc/html/rfc9659).

### Chunk-size experiment design

These are a **future test grid**, not measured largest useful chunks. Start with
one compression worker and two in-flight chunks; increase only after end-to-end
latency and aggregate memory accounting pass.

| Component RAM target | Candidate uncompressed chunks | Initial decoder-history cap |
| --- | --- | --- |
| 4 GiB | 64–256 KiB | 4 MiB |
| 8 GiB | 256 KiB–1 MiB | 8 MiB |
| 16 GiB | 1–4 MiB | 16 MiB |
| 32 GiB | 4–16 MiB | 32 MiB |
| 48 GiB | 16–64 MiB | 64 MiB |

Budget `queued chunks + active input/output + codec workspace/window + dictionary +
parser/record state + storage cache`. A transaction/record spanning chunks still
needs bounded carry state; tiny chunks do not magically bound a materialized JSON
DOM. Local RPC already caps response bodies at 64,000,000 bytes but can hold
compressed, encoded, decoded and parsed representations at different stages.

Choose the smallest chunk near the measured compression/throughput plateau, not
one that merely fits. Larger chunks can improve repeated-pattern coverage while
increasing time-to-first-record, corruption/replay scope and recovery latency.
Record ratio, CPU seconds, peak cgroup memory, p50/p95 decode latency and checked
round-trip hashes for each corpus, codec/window, chunk size and worker count.
Original evidence must survive a failed experimental encoding.

## 9. CPU, disk and bandwidth requirements

### CPU

The main question is serial replay plus cold-I/O latency on twenty physical
Broadwell cores, not total runnable threads. NUMA-remote account/cache access,
signature verification, program execution and simultaneous model training can all
matter. Use architecture-compatible builds; recompilation can address unsupported
instructions, not make older cores meet a throughput requirement.

For a conventional Agave node, S1 recommends at least 12 physical cores/24 threads
(16/32 for additional RPC workloads), 2.8 GHz+ and modern acceleration. Use those
and S2 as planning baselines, not evidence that this older 20-core host qualifies. No lower, reliable minimum was measured. A custom experiment might use
2–4 physical cores for ingestion/verification plus measured execution workers,
but that is a starting allocation, not a claim that four cores can replay mainnet.
Keep bot control/recovery scheduling responsive and test compression/replay/training
contention together. Move GUI rendering off Monolith or omit it.

### Storage

Upstream Agave recommends separate high-endurance NVMe capacity for accounts,
ledger and snapshots (1 TB, 1 TB and 500 GB respectively). Those are provisioning
guidelines, not verified current compressed dataset sizes. Monolith's actual
available capacity/topology is unverified; older local notes about free space
must not be treated as today's inventory. No destructive disk benchmark is proposed.

A smaller design still needs `live state + replay/repair horizon + snapshots +
compaction/rebuild temporary space + Freya evidence + recovery reserve`. Peak disk
space can exceed steady retained state during rebuild/compaction. Required IOPS
and throughput remain unknown until measured; cache-hit-only tests are insufficient.

### Existing network policy is a separate hard gate

Local source: `solana_network_budget.py` sets 120,000,000,000 normal and
150,000,000,000 hard budget bytes, with reserve authorization/safety margin.
`nix/solana-network-isolation.nix` defaults to 16 kbytes/s per direction.
`solana_network_lifecycle.py:start` limits a manually started session to about
55 seconds without automatic renewal. No deployed setting was probed or changed.

For a 30-day illustrative cycle, 120 GB allows about **46.3 kB/s combined average**
before any other counted work; the per-direction default shaper is tighter.
At nominal 16 kB/s, each direction allows about **41.5 GB/month** continuously.
This arithmetic is not measured mainnet demand. Snapshot/bootstrap, repair,
retransmissions and other traffic also consume the allowance. Current finite
sessions alone prevent a continuously connected node under the unchanged policy.

Firedancer's published recommended link is 1 Gbit/s. Link capacity is not the same
as sustained traffic volume or a measured minimum. A full peer node must demonstrate
that authentic live input plus protocol overhead fits the actual authorized rate;
a filtered upstream stream may be much smaller but changes the dependency model.
Do not equate a fast JSON-RPC probe with viable full-node network ingress.

Recovery requires `processing rate μ > incoming rate λ`; for a backlog B,
catch-up time is approximately `B / (μ − λ)` when units and behavior are comparable.
If μ ≤ λ, bounded queues only delay failure. Apply the same inequality to network
backfill and disk write/compaction capacity. No particular λ was measured here.

## 10. System-wide 64 GiB resource budget

The following is an **admission envelope for a future experiment**, not measured
minimum/recommended production consumption. It deliberately gives a candidate node
only 32 GiB peak. No independently validating node has demonstrated that bound.

| Unique allocation group | Illustrative steady GiB | Proposed peak ceiling GiB |
| --- | ---: | ---: |
| NixOS/kernel/networking/SSH and background services, excluding file cache below | 3 | 4 |
| Freya trading process, entry/exit models and embedded native RPC library | 1 | 2 |
| Indexer, candle builder and decode buffers | 2 | 4 |
| Learning worker: SVM/LSTM/neural/retrieval data and training workspace | 3 | 6 |
| Freya database/filesystem cache and storage buffers, excluding node pages | 2 | 4 |
| Optional private status/export process | 0.25 | 1 |
| Candidate node including its own state cache, page cache and locked/shared pages | 24 | 32 |
| **Budgeted total** | **35.25** | **53** |
| **Unallocated installed-memory headroom** | **28.75** | **11** |

This counts the Rust library inside its Python host process, not as another daemon.
The same applies to a future embedded node: ownership groups must be measured once,
even if their architecture is conceptually separate. Database file pages belong
in one accounting group, not both RSS and an extra cache allowance. ML allocations
are hypotheses; dataset/candidate growth and deserialization must be profiled before
these ceilings could be accepted.

| Operating phase | Modeled whole-system envelope | Main uncertainty |
| --- | --- | --- |
| Cold startup/snapshot reconstruction | Up to 53 GiB plus reserved headroom, **if all assigned ceilings hold** | Index build, archive/decode and allocator peaks can invalidate the model |
| Normal sync/trading/learning | Roughly 36 GiB illustrative steady; 53 GiB admitted peak | Actual hot set and simultaneous model data copies |
| Busy slots plus compression/training | Same 53 GiB envelope; scheduling must prevent oversubscription | CPU latency may fail before RAM |
| Crash/restart/catch-up/compaction | Same envelope, never an uncounted second node or second full state cache | Cold reads, duplicate recovery buffers and backlog |

These rows do not prove that actual worst-case use is bounded. A candidate exceeding
its group must fail the experiment, not borrow the entire machine or rely on swap.
No deployment may assume all installed RAM is usable: future inventory must use
actual MemTotal and include kernel/firmware reservations.

Use parent cgroup memory/peak/stat/events, swap and PSI accounting; shared-process
PSS can help attribute costs. Inspect HugeTLB accounting separately and reconcile
it with the selected kernel's memory accounting before adding totals. Do not sum
Firedancer tile RSS or count page cache twice. [S13](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

Future scheduling may serialize training-heavy work with cold replay/compaction
while retaining eligible observations durably. That is scheduling, not silently
disabling required learning or discarding data. A sustained mainnet design must
still make enough time for both workloads. Normal operation cannot depend on swap.
The existing Nix module's 48G/56G node settings are containment defaults, not a
successful sizing result and not sufficient whole-system planning for this budget.

## 11. Architecture comparison matrix

All lower-memory figures below are **proposed experiments**, not measured results.
Development estimates are rough team-planning ranges, not quotes or schedules.

| Approach | RAM/CPU/storage/network | Latency and recovery | Guarantees/dependencies | Effort and maintenance |
| --- | --- | --- | --- | --- |
| A. Optimized Agave fork | Upstream baseline well above budget; test 32–48 GiB only after disk-index/working-set analysis. Modern CPU/NVMe/full-chain ingress remain reference requirements | Full replay and repair; pruning determines backfill horizon | Can eliminate third-party RPC serving dependence if local consensus/execution and peer access actually work; checkpoint/peers remain bootstrap/delivery assumptions | 3–6 week feasibility study; major storage fork could take 6–12+ months and ongoing upgrade/security work |
| B. Minimal custom Rust full node | No demonstrated floor; 4/8/16/32 GiB are hypotheses, not credentials. Requires global logical state and full protocol input | Must implement compatible replay/finality, bounded recovery and repair | Potentially independent after validation; cannot skip arbitrary programs and retain full execution guarantees | Multi-person, multi-year protocol/runtime/security project; roughly 2–5+ years as planning risk, not a delivery promise |
| C. Hybrid verification client | 4–8 GiB private component target; subset storage and proof/signature work; feed-dependent network | Efficient streaming possible; requires manifest/proof-aware gap repair and replay retention | Can reduce trust only for explicitly validated properties. Upstream data/proof supply remains; removing JSON-RPC does not remove dependency | 1–3 months for a scoped collector/structural checker; much longer/unknown for genuine consensus-proof integration |
| D. Geyser/validator stream | External consumer could target 4–8 GiB; hosting the producer adds the entire validator footprint. Filtered bytes must be measured | Push delivery avoids repeated polling; resume support/window and completeness vary | Own verified producer gives local provenance; third-party producer still supplies execution/finality claims | 4–10 week adapter/evidence experiment; schema/producer-version and gap-repair maintenance |
| E. Enhanced Freya indexer | 4–8 GiB research target for bounded ingestion path, sharing the non-node budget; actual peak unmeasured | Existing checkpoints/decoders/recovery are reusable, but provider capacity still limits freshness | Retains current explicit source trust; cannot manufacture missing chain data or independent consensus | 2–6 week bounded data-plane experiment; lowest protocol-maintenance cost |

A/B are the approaches capable in principle of replacing third-party RPC with local
verification. D only does so when its **producer** is independently operated and
validated; colocating that producer costs node resources. C/E can avoid a particular
RPC endpoint by changing delivery without becoming full independent validators.

Geyser exports runtime account/slot/block/transaction information and can move query
load out of a validator; it does not eliminate that validator. Its interfaces carry
execution metadata, not merely signed transaction bytes. [S14](https://docs.anza.xyz/validator/geyser).

Yellowstone's inspected protocol includes commitment levels, slot statuses and
`from_slot`, but a field's existence does not guarantee retained replay or complete
coverage on a particular server. Preserve slot manifests, counts and reconcilable
ordering; reject incomplete block reconstruction. Raw/deshred feeds can arrive
before execution and cannot supply finalized successful swap receipts on their
own. [S15](https://github.com/rpcpool/yellowstone-grpc/blob/master/yellowstone-grpc-proto/proto/geyser.proto),
[S16](https://github.com/rpcpool/yellowstone-grpc/blob/master/README.md).

Other options researched, rather than assumed inferior or sufficient:

* **Tinydancer/diet RPC:** relevant proof/header/shred research. Its documented diet
  RPC adds custom methods to a full-node producer; no demonstrated sub-64 GiB full
  independent mainnet execution result was established from the inspected docs.
  [S17](https://docs.tinydancer.io/diet-rpc-node).
* **Jito ShredStream:** alternative authenticated delivery infrastructure to study;
  its provider/proxy dependency and raw-shred semantics are distinct from local
  execution/finality. This study did not benchmark it, establish entitlement/cost,
  or certify its recovery/completeness for Freya. [S18](https://docs.jito.wtf/lowlatencytxnfeed/).
* **ZK compression / compressed application accounts:** changes particular programs'
  state representation; it is not a transparent compressor for all existing Solana
  accounts or a replacement for the current network's validation rules.
  [S19](https://github.com/Lightprotocol/light-protocol).
* **An independently operated producer elsewhere:** could give local operational
  control over an upstream, but fails the requirement that the entire system fit
  on this one existing machine. It is a comparison option, not the selected plan.

## 12. Security, trust, licensing and maintainability

A private deployment removes public-serving overhead and attack surface, not
malicious peer/data input. Bound decompression, parser recursion, allocations,
queues and repair work. Preserve source/version identity, authenticated hashes,
quarantine, monotonically reconciled checkpoints and observed gaps. Corrupt input,
unsupported extensions or unknown finality must block admission; never fill gaps
with invented events or current oracle prices.

Use a separate unprivileged node identity if a future peer implementation needs
one; do not reuse the bot's funded wallet key. Node read access is not trade
submission authority. Keep signing/reconciliation/risk policy in its existing
boundary and keep the headless bot functional without monitoring software.

Licensing findings, not a legal certification:

* Agave at the inspected pin and Firedancer at the inspected tag carry Apache-2.0
  licenses. Retain notices, license copies and modification notices; inventory
  bundled dependencies and their licenses rather than assuming a repository label
  applies to every file. Firedancer's NOTICE documents third-party material.
  [S20](https://github.com/anza-xyz/agave/blob/825efd18292aff6ffcf9daa0f7612f21b3531a72/LICENSE),
  [S21](https://github.com/firedancer-io/firedancer/blob/v26.10.5/LICENSE),
  [S22](https://github.com/firedancer-io/firedancer/blob/v26.10.5/NOTICE).
* Apache's redistribution conditions and patent terms remain relevant when reusing
  code. The ASF/FSF compatibility guidance permits appropriate Apache-2.0/GPLv3
  combinations; the GNU compatibility matrix extends its relevant GPLv3 statements
  to AGPLv3. This is a basis for review, not permission to remove upstream licenses.
  [S23](https://www.apache.org/licenses/LICENSE-2.0),
  [S24](https://www.apache.org/licenses/GPL-compatibility),
  [S25](https://www.gnu.org/licenses/gpl-faq.en.html#AllCompatibility).
* Freya's local LICENSE is AGPL-3.0. Yellowstone's inspected LICENSE is also AGPL-3.0.
  Distribution and modified network-service source obligations must be reviewed
  against what is actually combined and deployed. Personal use does not itself
  require operating a public service; later redistribution remains a separate duty.
  [S26](https://github.com/rpcpool/yellowstone-grpc/blob/master/LICENSE).

Protocol features, executable programs, consensus transitions, snapshot formats,
source permissions and decoder layouts require ongoing maintenance. Existing
Agave/Firedancer code is the reference behavior to compare, not a permanent frozen
specification. A novel consensus verifier needs adversarial tests and independent
review, not just replay agreement on a few blocks. Rust/C FFI and privileged/XDP
integration need their own security review. No audit was performed here.

## 13. Headless deployment and optional remote access

The operator clarified that there are no public users to serve. The minimal target
is **node + bot + internal data boundary on the same host**: in-process handoff or
an owner-restricted Unix socket, no public RPC, no multi-user API and no required
Qt/QML/dashboard process. This avoids general RPC indexes, fan-out buffers and web
service infrastructure without removing chain verification.

Optional SSH administration can use a narrowly scoped local tunnel; WireGuard can
provide a private management network. These are optional administration mechanisms,
not a prerequisite for syncing. Neither carries wallet secrets through a website.
[S27](https://man.openbsd.org/ssh.1), [S28](https://www.wireguard.com/protocol/).

If desired later, render a Qt/QML status client on another computer against a small
read-only, authenticated status surface. A website subdomain could front a separate
sanitized status replica after an explicit future decision, but is unnecessary for
this node feasibility goal. Do not expose raw RPC, node administration or trading
controls. GUI/auth/customer-service development is outside the proposed proof of
concept and no such components were implemented.

## 14. Recommended architecture

For the **own-project objective**, investigate a private, bounded data service with
an interchangeable **verification engine**, not a new general-purpose RPC product:

`verified replay/explicitly trusted delivery → durable slot/receipt journal →
strict Pump/PumpSwap normalization + causal oracle history → candles/Freya`

The internal contract must identify whether each record came from locally verified
execution/consensus or an upstream claim. Storage ownership, fork/gap handling,
ordering and backpressure belong in the contract; an adapter cannot silently
upgrade trust. Keep the original evidence needed for re-verification and learning.

For the independent engine, first test a stripped **non-voting Agave** with disk
index/cache/recovery changes; use Firedancer's architecture for comparison, not its
unchanged memory settings. Retain execution and consensus libraries wherever
possible. A clean-sheet full Rust node is not justified before these experiments.

For useful interim infrastructure, a compact streaming/indexing engine can be an
independent Freya project while still honestly depending on an upstream producer.
That option is not asserted to be as independent as owning replay. The current
rate-limited RPC path remains a baseline for comparison, not an assumed adequate
long-term feed. No implementation choice is activated by this study.

## 15. Minimum viable future proof of concept

**No proof of concept was built.** Future work should fail the cheapest/highest-risk
gates first, without disrupting V3 paper validation or sharing its endpoint traffic.

1. **Policy and inventory gate:** obtain actual usable RAM, CPU flags, storage
   topology/space/endurance and an explicitly authorized continuous network budget.
   With the current finite sessions/rates unchanged, stop the independent-node
   experiment here. Do not download snapshots to discover that they exceed budget.
2. **Static allocation gate:** derive account-index capacity and snapshot/index-build
   allocations from the exact pinned sources. Compare real account-count metadata
   from an authorized source against the proposed 32 GiB node peak. A default
   allocation exceeding the budget rejects that configuration before startup.
3. **Smallest useful laboratory experiment:** reuse existing permitted retained
   receipts for bounded binary decode/journal/replay with a fixed-size queue; test
   disk-index lookup/cache primitives separately on a disposable corpus. A 4–8 GiB
   component target and one/two active decode workers are starting budgets only.
   This proves bounded data handling and storage costs, **not mainnet consensus**.
4. **Independent-engine experiment, only after those gates:** a reviewed non-voting
   upstream engine or narrowly modified account-storage backend, on isolated copies
   and an authenticated checkpoint/corpus. Dependencies include signature/PoH/shred
   handling, accounts/runtime/program cache, stake/finality and snapshot recovery;
   a market decoder alone is insufficient. Do not start by rebuilding all of them.
5. **Whole-system test:** exercise cold start, normal progress, busy workload,
   disconnect/catch-up and recovery with all required Freya functions present.
   Coordinate training-heavy windows rather than discarding outcomes. Compare the
   exact same input with the existing indexer and an established reference engine.

Measure global-state lookup metadata, cold-miss replay speed and input bytes before
optimizing compression ratios or adding a GUI. Synthetic key/index tests can reveal
allocator costs but cannot establish the real account working set or mainnet
throughput. Any later live test needs separate operator authorization and must not
consume V3's provider allowance through a competing collector.

## 16. Benchmarking and validation criteria

Proposed engineering gates, not measurements already met:

| Gate | Future pass criterion |
| --- | --- |
| Whole-system memory | Under actual usable RAM with at least 8 GiB operational headroom; no OOM events, normal swap dependence or uncounted huge/shared pages |
| Candidate allocation | Node peak ≤32 GiB in the initial design; exceeding it rejects the design rather than borrowing the host |
| Full replay correctness | Identical accepted state/root/receipt semantics to the version-matched reference on the complete test interval, including failures and unsupported cases |
| Finality | Explicitly demonstrate local consensus rules from the declared checkpoint, or retain an upstream-claimed label; no proof by API parameter alone |
| Continuous progress | At least 24-hour shadow test and subsequent multi-day soak; no unexplained slot/transaction gaps or persistently growing lag |
| Freya freshness | Existing index/swap freshness limits, full finalized window coverage and causal USD conversion remain enforced; no relaxed thresholds to pass |
| Repair/catch-up | A defined outage (initially 10 minutes) repairs within a declared bound (initially 20 minutes), with measured processing/ingress capacity above live production |
| Queue pressure | Peak traffic/backfill stays byte-bounded; every overflow is a recorded gap with verified recovery, never dropped evidence marked complete |
| Restart | No duplicate capital/outcomes, checkpoint jumps, lost original receipts or overwritten model/journal state |
| Codec integrity | Exact round-trip hashes; bounded malformed-frame/dictionary handling and reproducible resource figures |
| Paper/learning | Separately observe genuine eligible paper outcomes and learning; synthetic tests do not qualify profitability or live use |

Store source revisions, feature configuration, corpus hashes/rights, boot/checkpoint
trust, cgroup peaks, CPU time, storage I/O/latencies, accounted network bytes,
backlog/drain rate and decode latency. Record startup and recovery peaks separately
from averages. A full-node ingest rate must be compared with incoming chain data;
fast processing of only retained Pump events is not evidence of full replay speed.

Abandon/change direction if the account index alone exceeds budget; if cold replay
cannot drain backlog; if required raw ingress exceeds the authorized connection;
if repair requires unavailable history; or if a proposed light proof does not cover
Freya's needed receipts/completeness. Those outcomes favor a smaller explicitly
upstream-dependent data component, not a weaker definition of independent validation.

## 17. Unresolved blockers and risks

* No measured practical RAM floor, full-state working set, startup peak or replay
  throughput exists for this machine/candidate combination.
* Current managed-node session duration and network caps conflict with continuous
  peer operation. No unchanged-policy full-node feasibility claim is justified.
* SSD capacity, endurance, IOPS, thermal behavior and NUMA effects are unverified.
* Correct protocol/finality feature compatibility must be established for the exact
  future mainnet interval; source presence is not evidence of network activation.
* A small parser/indexer does not solve account proofs, selective-feed omission or
  authentic execution metadata. New proof availability must be established.
* Snapshot trust/size, index rebuild peaks, fork recovery and history retention may
  dominate both memory and transfer budgets.
* Full verified execution cannot simply skip unrelated transactions whose state
  effects may become dependencies of markets, token programs or consensus.
* Native RPC success and a lifecycle VM test do not prove sustained node operation,
  whole-system resource fit or paper/ML acceptance.

### Explicit answers to the final questions

1. **Lowest realistic footprint?** No demonstrated numeric full-verifier floor.
   A bounded private collector at 4–8 GiB is a worthwhile unmeasured target; 32 GiB
   is the proposed initial independent-engine research ceiling.
2. **Independent node beside Freya under 64 GiB?** Not established; standard
   documented configurations do not fit. A disk-heavy fork remains a hypothesis.
3. **Continuous mainnet throughput?** Not demonstrated; unchanged finite-session
   policy prevents continuous connectivity, regardless of RAM.
4. **Minimum CPU/storage/bandwidth?** No measured lower custom minimum. Use upstream
   modern-core/NVMe/large-RAM guidance as the conventional baseline; derive custom
   requirements from real cold replay, state size, input and recovery measurements.
5. **Compression or simplification?** First remove unnecessary public-serving/index
   overhead and memory duplication. Disk-backed index design may be decisive for a
   full verifier; compression helps retained bytes but does not remove required work.
6. **Guarantees retained/lost?** Full local replay/finality retains validation from
   the accepted bootstrap. A hybrid/filtered client retains only independently
   checked properties and trusts the producer for the rest.
7. **Eliminate third-party RPC?** In principle with a viable local verified engine
   and peer delivery. A third-party stream changes the service dependency rather
   than eliminating it; bootstrap/data peers still exist in either architecture.
8. **Best architecture?** A private Freya data plane with an explicit verifier
   boundary; reuse/tune established replay before contemplating a clean-sheet node.
9. **Smallest worthwhile experiment?** Static index/network feasibility gates,
   followed by offline bounded receipt handling and disposable disk-index/cache
   tests. No live snapshot/node experiment before those pass.
10. **Principal costs/uncertainties?** Global state/index working set, serial replay,
    cold I/O, ingress/repair rights and budgets, bootstrap/finality correctness and
    continual protocol maintenance. A full rewrite is a multi-year team risk.

## 18. Source references and inspection limits

Inline S1–S28 links identify official documentation, maintainer sources and standards.
Source paths in sections 3–4 were actually inspected through bounded repository-file
reads. Agave is pinned to the existing Freya commit; Firedancer to v26.10.5.
Yellowstone references its fetched master, not a certified/pinned deployment.
Selected fetched Git blob IDs provide additional content identity:

| File | Blob SHA |
| --- | --- |
| Agave accounts index | `2df9b64a280b359add684b7004da686716b87b18` |
| Agave accounts DB configuration | `792d6dc14d40357db9b0db8e208864969d655689` |
| Agave replay stage | `bda55599a7fed7d748fa8d19c7b8003412c5ba38` |
| Agave commitment service | `a02a051873e57a1589166b387924be8ec7499985` |
| Firedancer default configuration | `42efffdcdb0c46e88db3bdaa275bc0faa3e70cf0` |
| Firedancer replay tile | `dd8e2eb7072426e8d33d7236eed0ccf1376e3ecc` |
| Firedancer account DB header | `2fd407fc574bff85eae41ea432ae51e022fb6af1` |
| Yellowstone protocol | `d0f96616d515c978a07b16e6409c7706f4429a5a` |

Official web pages can change independently of these code pins. A bounded source
review cannot establish consensus/security correctness or every deployment minimum.
Some GNU pages timed out when opened; compatibility guidance was also available
through indexed GNU FAQ text and the fetched Apache guidance. No inaccessible file
is represented as inspected. No current mainnet account census, snapshots, ledger
bulk data, node benchmarks, hardware probes, tests, builds or services were run.

Only this research document is an intended repository change. Existing wallet,
transaction, model, training and provider-limit data and the V3 paper workflow
remain untouched. Implementation, GUI work and an actual proof of concept require
a later decision.
