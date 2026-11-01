"""Offline preflight and read-only pipeline health. Never opens RPC connections."""
import argparse
import json
import re
import sqlite3
import time
from pathlib import Path


def safe_reason(error):
    # Preserve useful validation messages without printing credential-bearing URLs.
    return re.sub(r'https?://\S+|wss?://\S+', '[endpoint]', str(error))[:240]


def validate_rpc_mode(config,mode):
    from urllib.parse import urlsplit
    enabled=[e for e in config.get('endpoints',[]) if e.get('enabled') is True]
    local=[e for e in enabled if urlsplit(e.get('url','')).hostname in ('127.0.0.1','::1')]
    remote=[e for e in enabled if e not in local]
    if mode not in ('remote','selfhosted','hybrid'):raise ValueError('Unknown RPC mode')
    if not enabled or (mode=='remote' and local) or (mode=='selfhosted' and remote) or (mode=='hybrid' and not (local and remote)):
        raise ValueError('Reviewed enabled RPC endpoints do not match selected mode')


def validate_config(config):
    from onchain_decode import Decoder
    from onchain_oracle import feed_account
    endpoints = [e for e in config.get('endpoints',[]) if e.get('enabled') is True]
    if not endpoints:
        raise ValueError('Enable an explicitly reviewed RPC source')
    if len({e['source_id'] for e in endpoints}) != len(endpoints):
        raise ValueError('Duplicate source identifiers')
    for endpoint in endpoints:
        policy = endpoint.get('policy',{})
        if policy.get('storage_verified') is not True or not policy.get('evidence'):
            raise ValueError('Reviewed storage rights required for every endpoint')
        if endpoint.get('backend','python') not in ('python','rust','auto'):
            raise ValueError('Unknown RPC backend')
        if endpoint.get('backend') == 'rust':
            from solana_rpc_compat import native_module
            native_module()
    decoder = Decoder(backend=endpoints[0].get('event_backend','python'),
                      schema_directory=config.get('schema_directory'))
    versions = config.get('reviewed_program_versions',{})
    for program,spec in decoder.specs.items():
        ranges = versions.get(program,[])
        if not ranges:
            raise ValueError('Reviewed deployment ranges missing for '+program)
        previous = -1
        for row in sorted(ranges,key=lambda v:v['from_slot']):
            if (any(type(row.get(k)) is not int for k in ('from_slot','through_slot','deployment_slot'))
                or not 0 <= row['deployment_slot'] <= row['from_slot'] <= row['through_slot']
                or row['from_slot'] <= previous or row.get('idl_hash') != spec['hash']):
                raise ValueError('Invalid, overlapping or schema-mismatched deployment range')
            previous = row['through_slot']
    oracle = config.get('oracle')
    if oracle:
        interval = oracle.get('interval_seconds')
        if type(interval) is not int or not 10 <= interval <= 60:
            raise ValueError('Oracle interval must be 10..60 seconds')
        source = next((e for e in endpoints if e['source_id']==oracle.get('source_id')),None)
        if source is None:
            raise ValueError('Oracle source must be an enabled RPC source')
        policy = source['policy']
        limit = policy.get('oracle_max_confidence_bps')
        if (policy.get('usd_conversion_verified') is not True
            or policy.get('oracle_data_rights_verified') is not True
            or type(limit) is not int or not 0 <= limit <= 10000):
            raise ValueError('Reviewed oracle rights, USD method and confidence policy required')
        feeds = oracle.get('feeds',[])
        if not 1 <= len(feeds) <= 4 or len({f['mint'] for f in feeds}) != len(feeds):
            raise ValueError('One to four unique reviewed USD feeds required')
        for feed in feeds:
            feed_account(feed,feed.get('shard',0))
    return decoder


def health(database, mints=(), now=None):
    from onchain_store import EventStore
    from onchain_market import OnchainMarket
    now = time.time() if now is None else now
    store = EventStore(database,readonly=True)
    watermark = store.watermark()
    blockers = []
    if watermark is None or not 0 <= now-watermark <= 30:
        blockers.append('Finalized index missing or older than 30 seconds')
    coverage = {'complete':False}
    # Finality trails wall time: measure coverage through the available watermark.
    if watermark is not None:
        coverage = store.coverage(watermark-3600,watermark)
    if not coverage['complete']:
        blockers.append('Complete finalized hour unavailable')
    with store.connect() as db:
        issues = [dict(row) for row in db.execute("SELECT slot,issues FROM blocks WHERE issues!='[]' ORDER BY slot DESC LIMIT 3")]
        conversions = [dict(row) for row in db.execute('SELECT mint,max(time) AS latest FROM conversions GROUP BY mint')]
        row = db.execute("SELECT payload FROM telemetry WHERE name='oracle'").fetchone() if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='telemetry'").fetchone() else None
    from wallet_watch import SOL
    try:
        store.conversion(SOL,int(now))
    except ValueError as error:
        blockers.append(safe_reason(error))
    markets = {}
    if not mints:
        with store.connect() as db:
            mints = [r[0] for r in db.execute("SELECT DISTINCT mint FROM events WHERE kind='swap' AND time>=? AND slot IN (SELECT slot FROM blocks WHERE issues='[]') LIMIT 5",(int(now)-30,))]
        if not mints:
            blockers.append('No recent verified swap markets')
    for mint in mints:
        try:
            observation = OnchainMarket(store).observation(mint,now)
            errors = observation['data_errors']
            if not 0 <= now-observation['observed_at'] <= 30:
                errors = errors+['Latest swap older than 30 seconds']
            markets[mint] = {'price_as_of':observation['observed_at'], 'errors':errors,
                             'learning_allowed':observation['data_rights']['learning_allowed']}
            if errors:
                blockers.append('Market data incomplete: '+mint)
        except (ValueError,KeyError) as error:
            markets[mint] = {'error':safe_reason(error)}
            blockers.append('Market unavailable: '+mint)
    return {'ready':not blockers, 'blockers':blockers, 'index':store.metrics(),
            'progress':store.progress(), 'coverage':coverage, 'recent_issues':issues,
            'conversions':conversions, 'oracle':json.loads(row[0]) if row else None, 'markets':markets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--rpc-mode',choices=('remote','selfhosted','hybrid'))
    parser.add_argument('--db',type=Path)
    parser.add_argument('--mint',action='append',default=[])
    parser.add_argument('--require-ready',action='store_true')
    args = parser.parse_args()
    report = {}
    try:
        if args.config:
            config = json.loads(args.config.read_text())
            if args.rpc_mode:validate_rpc_mode(config,args.rpc_mode)
            validate_config(config)
            report['configuration']='reviewed fields present; runtime admission not proven'
            report['backends']=[{k:e.get(k,'python') for k in ('backend','event_backend')}
                                for e in config['endpoints'] if e.get('enabled') is True]
        if args.db:
            report.update(health(args.db,args.mint))
        if not args.config and not args.db:
            parser.error('--config or --db required')
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,sqlite3.Error) as error:
        report.update(ready=False,blockers=[safe_reason(error)])
    print(json.dumps(report,allow_nan=False))
    if report.get('ready') is False or args.require_ready and not args.db:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
