# Fresh native runtime after confirmed data loss

Normal startup always starts at slot 0 when no retained checkpoint/window/slot
schedule exists. Valid retained progress resumes automatically; corrupt progress
blocks. Identity files alone do not select a resume slot. Bootstrap below does
not reset follower progress, identity, network accounting, wallets or training.

Use this only after confirmed loss of **both** the private RPC configuration and
provider ledger. `deployment/native-mainnet.reviewed.json` is a portable copy of
the existing development configuration, including its original policy evidence
and deployment ranges; it is not newly validated or a live-order authorization.
Review it before use. Never extend its deployment ranges back to genesis or mark
additional policies verified to make historical decoding pass.
Its decoder hashes refer to the prior schemas and therefore do not currently
qualify full ingestion. Bootstrap and native host preparation validate only the
reviewed source/transport needed for metadata planning. They copy those review
records unchanged and explicitly grant no decoder qualification. Full indexer and
observer startup continue rejecting the stale pins until genuine deployment/schema
requalification; native acquisition progress is not receipt-verified ingestion.

On Monolith, after transferring this checkout, as `link`:

```bash
cd ~/repos/Freya
nix develop --no-write-lock-file .#native-mainnet
```

Inside that shell, run each command only after the preceding command succeeds:

```bash
systemctl is-active freya-node.service freya-peer-discovery.service solana-budget-network.service
# All three must be inactive/failed (is-active returns nonzero for inactive).
python prepare_native_runtime.py --source deployment/native-mainnet.reviewed.json --acknowledge-lost-provider-history
python -m unittest discover -s tests -p test_native_runtime_bootstrap.py
python -m unittest discover -s tests -p test_native_window_planning.py
refresh_dir="state/mainnet-refresh-$(date -u +%Y%m%dT%H%M%SZ)"
python prepare_mainnet_host.py --refresh --output "$refresh_dir"
diff -u /etc/nixos/freya-mainnet.nix "$refresh_dir/freya-mainnet.nix"
```

Use `--refresh` only when `systemctl show ... -p LoadState` reports all three
Freya units loaded. If a base-host rebuild removed them (`not-found`), omit
`--refresh` to prepare a fresh service installation, preserving the retained
identity and network-budget ledger.

Bootstrap makes no network calls. It creates owner-only files exclusively and
refuses existing targets, including symlinks. Policy flags are copied unchanged.
It records a new accounting epoch with explicitly **unavailable prior history**,
and applies the existing 60-second throttle margin. This does not establish that a lost remote
denial or longer cooldown has cleared: resolve any known provider restriction
before running a network session. No existing budget ledger is reset.

If interrupted after ledger publication, retain that ledger. If only the config
is missing, install the reviewed source without replacing an existing target:

```bash
python -c 'import os,pathlib; source=pathlib.Path("deployment/native-mainnet.reviewed.json"); fd=os.open("onchain_pipeline.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); stream=os.fdopen(fd,"wb"); stream.write(source.read_bytes()); stream.flush(); os.fsync(stream.fileno()); stream.close()'
```

Review the generated diff (exit 1 means differences). Retain all existing caps,
ledger paths, user identity, isolation, and `Restart=no`. Then:

```bash
sudo cp -a /etc/nixos/freya-mainnet.nix "$refresh_dir/freya-mainnet.before.nix"
sudo install -m 0644 "$refresh_dir/freya-mainnet.nix" /etc/nixos/freya-mainnet.nix
sudo nixos-rebuild build -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
sudo nixos-rebuild dry-activate -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
sudo nixos-rebuild test -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
sudo systemctl show freya-peer-discovery.service solana-budget-network.service -p ExecStart -p Restart -p ActiveState
```

After the reported `provider_not_before` timestamp and any longer known provider
restriction, run one bounded session, without automatic retry:

```bash
sudo systemctl start freya-peer-discovery.service
sudo journalctl -u freya-peer-discovery.service -u solana-budget-network.service --since '-5 minutes' --no-pager
sudo cat /var/lib/freya-node/follow/planning-error.json
sudo cat /var/lib/freya-node/follow/planning-supervisor.json
```

Some diagnostic files may be absent if their stage was not reached. Use the
checkpoint/validated-shred inspection in
[the shutdown review](native_shutdown_review_20261104.md#read-only-evidence-collection-on-monolith).
Success requires validated shred persistence and checkpoint progress, not merely
a clean exit. The existing history-floor gate can reject genesis if the selected
RPC has pruned it; never skip to its available head or claim genesis was ingested.
Such a rejection requires an actually available archival source, reviewed before
changing provider configuration. This bootstrap does not supply archival data.

Rollback configuration, preserving all newly recorded accounting and data:

```bash
sudo systemctl stop freya-peer-discovery.service freya-node.service solana-budget-network.service
sudo install -m 0644 "$refresh_dir/freya-mainnet.before.nix" /etc/nixos/freya-mainnet.nix
sudo nixos-rebuild test -I nixos-config=/etc/nixos/freya-diagnostic-host.nix
```

The previous installed add-on may reference lost runtime files; rollback restores
configuration, not those files. Leave services stopped until its inputs exist.
