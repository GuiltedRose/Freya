"""Read-only deployment qualification and bounded finalized feed sampling."""
import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from onchain_decode import Decoder
from onchain_ingest import Ingester, read_program_deployments
from onchain_store import EventStore
from pipeline_runtime import resolve_indexer
from provider_limits import BUDGET
from run_onchain_indexer import bootstrap_start
from runtime_health import safe_reason, validate_config
from solana_rpc_compat import check_boundary, close_rpc_clients, make_rpc
from sync_policy import slot


def deployment_candidate(rpc, decoder, selected_receipts):
    rpc.validate()
    deployments = read_program_deployments(rpc, decoder.specs)
    receipts = {}
    for program, spec in decoder.specs.items():
        signatures = selected_receipts.get(spec['venue'])
        if signatures is None:
            rows = rpc.call('getSignaturesForAddress', [program, {'commitment': 'finalized', 'limit': 24}])
            signatures = [row['signature'] for row in rows if row.get('err') is None][:4]
        for signature in signatures:
            tx = rpc.call('getTransaction', [signature, {
                'commitment': 'finalized', 'encoding': 'json', 'maxSupportedTransactionVersion': 1,
            }])
            meta = tx.get('meta') if tx else None
            if not isinstance(meta, dict) or 'err' not in meta or meta['err'] is not None:
                continue
            observed = slot(tx['slot'])
            if observed < deployments[program] or tx['transaction']['signatures'][0] != signature:
                continue
            events, issues = decoder.transaction(tx, observed, 0, tx.get('blockTime'))
            if not issues and any(e['kind'] == 'swap' and e['venue'] == spec['venue'] for e in events):
                receipts[program] = {'signature': signature, 'slot': observed}
                break
        if program not in receipts:
            raise ValueError('No issue-free finalized swap receipt for ' + spec['venue'])
    head = slot(rpc.call('getSlot', [{'commitment': 'finalized'}]))
    if head < max(r['slot'] for r in receipts.values()) or deployments != read_program_deployments(rpc, decoder.specs):
        raise ValueError('Finalized deployment evidence changed during sampling')
    return {
        'status': 'candidate_requires_review',
        'source_id': rpc.source,
        'receipts': receipts,
        'candidate_program_versions': {
            program: [{
                'from_slot': head,
                'through_slot': 2**63 - 1,
                'deployment_slot': deployment,
                'idl_hash': decoder.specs[program]['hash'],
                'evidence': 'Finalized loader and Rust/Python receipt comparison: ' + receipts[program]['signature'],
            }]
            for program, deployment in deployments.items()
        },
    }


def sample_feed(rpc, policy, decoder, rounds, batch, interval):
    with tempfile.TemporaryDirectory(prefix='freya-feed-check-') as directory:
        store = EventStore(Path(directory) / 'index.sqlite')
        store.register_source(rpc.source, policy)
        start = bootstrap_start(store, [rpc], recent=True)
        ingester = Ingester(store, [rpc], decoder=decoder)
        samples = []
        for index in range(rounds):
            if index:
                time.sleep(interval)
            result = ingester.step(start if index == 0 else None, batch)
            progress = result['progress']
            samples.append({key: progress[key] for key in (
                'finalized_head', 'head_sampled_at', 'next_slot', 'lag_slots', 'processed_slots',
            )})
        head_growth = samples[-1]['finalized_head'] - samples[0]['finalized_head']
        cursor_growth = samples[-1]['next_slot'] - samples[0]['next_slot']
        return {
            'status': 'feed_sample',
            'source_id': rpc.source,
            'samples': samples,
            'keeping_up': head_growth > 0 and cursor_growth >= head_growth
                          and samples[-1]['lag_slots'] <= batch
                          and all(sample['processed_slots'] > 0 for sample in samples),
            'paper_ready': False,
        }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('deployments', 'feed', 'stage'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--source-id')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--batch-slots', type=int, default=8)
    parser.add_argument('--interval', type=int, default=5)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--review')
    parser.add_argument('--output-config', type=Path)
    parser.add_argument('--receipt', action='append', default=[], metavar='VENUE=SIGNATURE')
    args = parser.parse_args()
    if not 2 <= args.rounds <= 20 or not 1 <= args.batch_slots <= 64 or not 1 <= args.interval <= 300:
        parser.error('Use 2–20 rounds, 1–64 slots and a 1–300 second interval')
    rpc = None
    try:
        raw = args.config.read_bytes()
        fingerprint = hashlib.sha256(raw).hexdigest()
        config = json.loads(raw)
        if args.mode == 'stage':
            if not (args.candidate and args.review and args.review.strip() and args.output_config):
                parser.error('stage requires --candidate, --review and --output-config')
            candidate = json.loads(args.candidate.read_text())
            if candidate.get('status') != 'candidate_requires_review' or candidate.get('config_sha256') != fingerprint:
                raise ValueError('Candidate does not match the current configuration; rerun deployments')
            versions = candidate['candidate_program_versions']
            if not any(row.get('enabled') is True and row['source_id'] == candidate['source_id']
                       for row in config['endpoints']):
                raise ValueError('Candidate source is no longer enabled')
            for rows in versions.values():
                for row in rows:
                    row['evidence'] += '; operator review: ' + args.review.strip()
            config['reviewed_program_versions'] = versions
            validate_config(resolve_indexer(config, os.environ))
            with tempfile.TemporaryDirectory(prefix='.freya-pins-', dir=args.output_config.parent) as directory:
                staged = Path(directory) / 'indexer.json'
                with staged.open('x') as stream:
                    json.dump(config, stream, indent=2, allow_nan=False)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(staged, args.output_config)
            directory_fd = os.open(args.output_config.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            print(json.dumps({'status': 'reviewed_config_staged', 'path': str(args.output_config)}))
            return 0
        if not Path(BUDGET.path).is_file():
            raise ValueError('Set CRYPTO_BOT_PROVIDER_LIMITS to the retained shared ledger')
        config = resolve_indexer(config, os.environ)
        sources = [row for row in config['endpoints'] if row.get('enabled') is True
                   and (args.source_id is None or row['source_id'] == args.source_id)]
        if len(sources) != 1:
            raise ValueError('Select one reviewed enabled source with --source-id')
        source = sources[0]
        check_boundary(source)
        rpc = make_rpc(source)
        decoder = Decoder(backend='compare', schema_directory=config.get('schema_directory'))
        from solders.signature import Signature
        selected = {}
        for value in args.receipt:
            venue, separator, signature = value.partition('=')
            if not separator or venue not in {spec['venue'] for spec in decoder.specs.values()}:
                raise ValueError('Receipt must be supplied as VENUE=SIGNATURE')
            signatures = selected.setdefault(venue, [])
            if len(signatures) >= 4:
                raise ValueError('At most four supplied receipts per venue')
            signatures.append(str(Signature.from_string(signature)))
        result = (deployment_candidate(rpc, decoder, selected) if args.mode == 'deployments' else
                  sample_feed(rpc, source['policy'], decoder, args.rounds, args.batch_slots, args.interval))
        if args.mode == 'deployments':
            result['config_sha256'] = fingerprint
        print(json.dumps(result, allow_nan=False))
        return int(result.get('keeping_up') is False)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        print(json.dumps({'status': 'blocked', 'reason': safe_reason(error)}))
        return 1
    finally:
        if rpc is not None:
            close_rpc_clients([rpc])


if __name__ == '__main__':
    raise SystemExit(main())
