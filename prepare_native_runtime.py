"""Explicit offline bootstrap after runtime data loss; never runs services or RPC."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import time

from pipeline_runtime import read_json, resolve_indexer
from provider_limits import ProviderBudget, THROTTLE_MARGIN_SECONDS

ROOT = Path(__file__).resolve().parent


def bootstrap(source, config, ledger, *, acknowledge_loss=False):
    if not acknowledge_loss:
        raise ValueError('Explicit acknowledgement of lost provider history is required')
    source, config, ledger = map(Path, (source, config, ledger))
    if config.absolute() == ledger.absolute():
        raise ValueError('Configuration and ledger need distinct paths')
    # Never reuse this operation to clear cooldowns, denials or existing data.
    for target in (config, ledger):
        if target.exists() or target.is_symlink():
            raise ValueError('Existing runtime file must be retained: '+str(target))
    document = read_json(source)
    resolved = resolve_indexer(document, os.environ)
    from runtime_health import validate_config
    validate_config(resolved)
    endpoints = [e for e in resolved.get('endpoints', []) if e.get('enabled') is True]
    if len(endpoints) != 1 or endpoints[0].get('backend') != 'rust':
        raise ValueError('One reviewed Rust RPC source is required')
    for target in (config, ledger):
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Prepare both files privately before exclusive publication. Hard links refuse
    # replacement even if a competing process creates a target after preflight.
    with tempfile.TemporaryDirectory(prefix='.native-bootstrap-', dir=ledger.parent) as scratch:
        staged_ledger = Path(scratch)/'provider.sqlite'
        budget = ProviderBudget(staged_ledger)
        budget.identity(endpoints[0]['url'])
        now = time.time()
        with closing(budget.connect()) as db, db:
            # Missing history is not evidence that a provider cooldown expired.
            # Apply the existing throttle margin to this new accounting epoch;
            # this cannot certify that an unknown older restriction has expired.
            db.execute('INSERT INTO providers VALUES(?,?,?,?,?)',
                       (budget.key(endpoints[0]['url']), now+THROTTLE_MARGIN_SECONDS,
                        now+THROTTLE_MARGIN_SECONDS, 0, 0))
            db.execute('CREATE TABLE bootstrap_provenance(at REAL NOT NULL,reason TEXT NOT NULL)')
            db.execute('INSERT INTO bootstrap_provenance VALUES(?,?)',
                       (now, 'operator_acknowledged_lost_provider_history; prior history unavailable'))
        with staged_ledger.open('rb') as stream:
            os.fsync(stream.fileno())
        fd, name = tempfile.mkstemp(prefix='.native-config-', dir=config.parent)
        staged_config = Path(name)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(document, stream, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.link(staged_ledger, ledger)
            # If interrupted or publication fails, retain the new accounting
            # file. Never delete a ledger another process may have opened.
            os.link(staged_config, config)
            for directory in {config.parent, ledger.parent}:
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            staged_config.unlink(missing_ok=True)
    return {'config': str(config), 'ledger': str(ledger),
            'provider_not_before': now+THROTTLE_MARGIN_SECONDS, 'services_started': False,
            'network_probes': False, 'historical_accounting_recovered': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True,
                        help='Explicitly reviewed source configuration; copied without changing policy flags')
    parser.add_argument('--config', type=Path, default=ROOT/'onchain_pipeline.json')
    parser.add_argument('--ledger', type=Path, default=ROOT/'state/onchain/provider_limits.sqlite')
    parser.add_argument('--acknowledge-lost-provider-history', action='store_true')
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error('Run as the existing non-root collector, not sudo')
    import subprocess
    for unit in ('freya-node.service', 'freya-peer-discovery.service', 'solana-budget-network.service'):
        state = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                               check=True, capture_output=True, text=True).stdout.strip()
        if state not in ('inactive', 'failed'):
            parser.error('Services must be installed and stopped: '+unit)
    try:
        print(json.dumps(bootstrap(args.source, args.config, args.ledger,
                                  acknowledge_loss=args.acknowledge_lost_provider_history), indent=2))
    except Exception as error:
        # Source configuration may contain private provider URLs.
        raise SystemExit('Bootstrap stopped ('+type(error).__name__+
                         '); inspect existing files before retrying; no service started') from None


if __name__ == '__main__':
    main()
