"""Bounded RPC planning only; no receipt requests, wallets, or index writes."""
import argparse
from collections import Counter
import json
import ipaddress
import os
from pathlib import Path
import time
import uuid
import math


def planning_error(error):
    """Only bounded categories/codes, never exception text or endpoint details."""
    record={'schema':'freya-native-planning-error-v1','at':time.time(),
            'error_type':type(error).__name__ if type(error).__name__ in
            ('ValueError','PermissionError','FileNotFoundError','ConnectionError','SourceAccessBlocked',
             'ProviderBackoff','RpcEvidenceError','TimeoutError','RuntimeError') else 'PlannerError'}
    method=getattr(error,'freya_rpc_method',None)
    if method in ('getGenesisHash','getSlot','getSlotLeaders','getBlocks','getFirstAvailableBlock','getClusterNodes'):
        record['rpc_method']=method
    category=getattr(error,'category',None)
    if category in ('connect','connect_timeout','request_timeout','transport','throttled','cancelled',
                    'transport_unavailable','authorization_denied','malformed_evidence','history_unavailable',
                    'remote_rpc_error','provider_unavailable','http_error'):
        record['category']=category
    for name in ('http_status','socket_errno','rpc_code'):
        value=getattr(error,name,None)
        if type(value) is int and -2147483648<=value<=2147483647:record[name]=value
    phase = getattr(error, 'phase', None)
    if phase in ('connect', 'request_send', 'response_headers', 'response_body', 'client_state'):
        record['phase'] = phase
    for name in ('elapsed_ms', 'bytes_received'):
        value = getattr(error, name, None)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**53:
            record[name] = value
    if 'category' not in record:
        record['category'] = ('throttled' if type(error).__name__ == 'ProviderBackoff'
                              else 'authorization_denied' if type(error).__name__ == 'SourceAccessBlocked'
                              else 'request_timeout' if isinstance(error, TimeoutError)
                              else 'invalid_rpc_metadata' if isinstance(error, ValueError)
                              else 'planner_process_failure')
    return record


def atomic_diagnostic(path, record):
    temporary = path.with_suffix('.pending')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


class PlanningProgress:
    """One bounded snapshot, replaced atomically; no RPC values or exception text."""
    def __init__(self, output, run_id):
        self.path = output.parent / 'planning-progress.json'
        self.started = time.monotonic()
        self.record = {'schema': 'freya-native-planning-progress-v1', 'run_id': run_id,
                       'started_at': time.time(), 'stages': []}
        self.stage_started = self.started
        self.write('started')

    def write(self, status, category=None):
        self.record.update(at=time.time(), elapsed_seconds=time.monotonic()-self.started,
                           stage_elapsed_seconds=time.monotonic()-self.stage_started, status=status)
        if category: self.record['category'] = category
        atomic_diagnostic(self.path, self.record)

    def begin(self, stage, method=None):
        self.stage_started = time.monotonic()
        self.record.update(stage=stage, rpc_method=method, stage_started_at=time.time())
        self.write('running')

    def complete(self):
        self.record['stages'].append({'stage': self.record['stage'],
            'rpc_method': self.record['rpc_method'], 'started_at': self.record['stage_started_at'],
            'elapsed_seconds': time.monotonic()-self.stage_started, 'status': 'completed'})
        self.write('stage_completed')


def prepare(config_path, output, start=None, run_id=None):
    run_id = run_id or uuid.uuid4().hex
    progress = PlanningProgress(output, run_id)
    # Replace stale failure classification before configuration, imports or RPC.
    atomic_diagnostic(output.parent / 'planning-error.json', {
        'schema': 'freya-native-planning-error-v1', 'at': time.time(),
        'run_id': run_id, 'category': 'in_progress'})
    try:
        _prepare(config_path, output, start, progress)
    except SystemExit as error:
        progress.write('caught_up' if error.code == 75 else 'failed', 'caught_up' if error.code == 75 else 'planner_process_failure')
        raise
    except Exception as error:
        record = planning_error(error)
        record.update(run_id=run_id, stage=progress.record.get('stage'),
                      rpc_method=progress.record.get('rpc_method'))
        atomic_diagnostic(output.parent / 'planning-error.json', record)
        progress.write('failed', record['category'])
        raise
    else:
        progress.write('completed', 'planner_success')
        atomic_diagnostic(output.parent / 'planning-error.json', {
            'schema': 'freya-native-planning-error-v1', 'at': time.time(),
            'run_id': run_id, 'category': 'none'})


def bootstrap_nodes(cluster, version):
    """Public IPv4 gossip candidates from reviewed RPC claims, not peer proofs."""
    from solders.pubkey import Pubkey
    selected, addresses, identities = [], set(), set()
    for row in cluster:
        if not isinstance(row, dict) or type(row.get('shredVersion')) is not int or row['shredVersion'] != version:
            continue
        try:
            identity = str(Pubkey.from_string(row['pubkey']))
            host, separator, port = row['gossip'].rpartition(':')
            address = ipaddress.IPv4Address(host)
            if (not separator or not address.is_global or address.is_multicast or address.is_reserved
                    or not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535):
                continue
            socket = f'{address}:{int(port)}'
        except (KeyError, TypeError, AttributeError, ValueError):
            continue
        if socket in addresses or identity in identities:
            continue
        selected.append({'identity': identity, 'gossip': socket})
        addresses.add(socket); identities.add(identity)
        if len(selected) == 4:
            break
    return selected


def _prepare(config_path, output, start, progress):
    from pipeline_runtime import read_json, resolve_indexer
    from solana_rpc_compat import make_rpc, close_rpc_clients
    from solders.pubkey import Pubkey
    progress.begin('rpc_validation', 'getGenesisHash')
    config = resolve_indexer(read_json(config_path), {'SOLANA_RPC_URL': os.environ.get('SOLANA_RPC_URL', '')})
    rows = [e for e in config.get('endpoints', []) if e.get('enabled') is True]
    if len(rows) != 1 or rows[0].get('backend') != 'rust':
        raise ValueError('One explicitly reviewed Rust RPC required')
    rpc = make_rpc(rows[0])
    def call(stage,method,params):
        progress.begin(stage,method)
        try:
            result=rpc.call(method,params)
            progress.complete()
            return result
        except Exception as error:
            error.freya_rpc_method=method
            raise
    try:
        from provider_limits import BUDGET
        identity = BUDGET.identity(rows[0]['url'])
        try:
            rpc.validate()  # Existing mainnet genesis, policy, boundary and shared pacing.
        except Exception as error:
            error.freya_rpc_method='getGenesisHash'
            raise
        progress.complete()
        head = call('finalized_head', 'getSlot', [{'commitment': 'finalized'}])
        if type(head) is not int or not 0 <= head < 2**63:
            raise ValueError('Invalid finalized head')
        start = max(0, head - 3) if start is None else start
        if type(start) is int and start > head:
            raise SystemExit(75)  # Caught up; wait under the same lease, never skip forward.
        if type(start) is not int or not 0 <= start <= head:
            raise ValueError('Window start is beyond finalized head')
        end = min(head, start + 3)
        def check_history(stage):
            floor = call(stage, 'getFirstAvailableBlock', [])
            if type(floor) is not int or not 0 <= floor <= start:
                raise ValueError('Historical window unavailable; preserve unresolved gap')
        check_history('history_floor')
        leaders = call('leader_schedule', 'getSlotLeaders', [start, end - start + 1])
        slots = call('finalized_manifest', 'getBlocks', [start, end, {'commitment': 'finalized'}])
        check_history('history_recheck')  # Retention can advance while the manifest is being fetched.
        progress.begin('manifest_validation')
        if (not isinstance(leaders, list) or len(leaders) != end - start + 1
                or not isinstance(slots, list) or any(type(s) is not int for s in slots)
                or slots != sorted(set(slots)) or any(not start <= s <= end for s in slots)):
            raise ValueError('Invalid leader schedule or finalized manifest')
        for leader in leaders:
            Pubkey.from_string(leader)
        progress.begin('cluster_metadata')
        cached = None
        if output.exists():
            prior = read_json(output)
            if (prior.get('schema') == 'freya-native-window-v1' and prior.get('source_identity') == identity
                    and 0 <= time.time() - prior.get('shred_version_at', 0) <= 300
                    and type(prior.get('shred_version')) is int and 1 <= prior['shred_version'] <= 65535):
                cached = prior['shred_version']
        reuse = cached and isinstance(prior.get('bootstrap_nodes'), list) and prior['bootstrap_nodes']
        cluster = [] if reuse else call('cluster_nodes', 'getClusterNodes', [])
        progress.begin('cluster_metadata')
        if not isinstance(cluster, list) or len(cluster) > 20000:
            raise ValueError('Invalid cluster metadata')
        versions = Counter(row.get('shredVersion') for row in cluster if isinstance(row, dict)
                           and type(row.get('shredVersion')) is int and 1 <= row['shredVersion'] <= 65535)
        if not versions and cached is None:
            raise ValueError('Compatible shred version unavailable')
        version = cached or versions.most_common(1)[0][0]
        version_at = prior['shred_version_at'] if cached else time.time()
        peers = prior['bootstrap_nodes'] if reuse else bootstrap_nodes(cluster, version)
        plan = {'schema': 'freya-native-window-v1', 'planning_run_id': progress.record['run_id'], 'at': time.time(), 'start': start, 'end': end,
                'observed_finalized_head': head, 'genesis': '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d',
                'source_id': rpc.source, 'source_identity': identity,
                'verification': 'reviewed_rpc_claim', 'shred_version': version,
                'shred_version_at': version_at,
                'bootstrap_nodes': peers, 'bootstrap_provenance': 'reviewed_rpc_claim',
                'slots': [{'slot': slot, 'leader': leaders[slot-start]} for slot in slots],
                'rpc_reported_skipped_slots': [slot for slot in range(start, end+1) if slot not in slots]}
        progress.begin('window_publication')
        temporary = output.with_suffix('.pending')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w') as stream:
            json.dump(plan, stream, allow_nan=False); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, output)
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        progress.complete()
    finally:
        close_rpc_clients([rpc])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=int)
    parser.add_argument('--run-id', type=lambda value: str(uuid.UUID(value)).replace('-', ''))
    args = parser.parse_args()
    try:
        prepare(args.config, args.output, args.start, args.run_id)
    except Exception:
        raise SystemExit('Native window planning failed; see private planning-error.json') from None


if __name__ == '__main__':
    main()
