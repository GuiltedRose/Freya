"""Bounded RPC planning only; no receipt requests, wallets, or index writes."""
import argparse
from collections import Counter
import json
import ipaddress
import os
from pathlib import Path
import time


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


def prepare(config_path, output, start=None):
    from pipeline_runtime import read_json, resolve_indexer
    from solana_rpc_compat import make_rpc, close_rpc_clients
    from solders.pubkey import Pubkey
    config = resolve_indexer(read_json(config_path), {'SOLANA_RPC_URL': os.environ.get('SOLANA_RPC_URL', '')})
    rows = [e for e in config.get('endpoints', []) if e.get('enabled') is True]
    if len(rows) != 1 or rows[0].get('backend') != 'rust':
        raise ValueError('One explicitly reviewed Rust RPC required')
    rpc = make_rpc(rows[0])
    try:
        from provider_limits import BUDGET
        identity = BUDGET.identity(rows[0]['url'])
        rpc.validate()  # Existing mainnet genesis, policy, boundary and shared pacing.
        head = rpc.call('getSlot', [{'commitment': 'finalized'}])
        if type(head) is not int or not 0 <= head < 2**63:
            raise ValueError('Invalid finalized head')
        start = max(0, head - 3) if start is None else start
        if type(start) is int and start > head:
            raise SystemExit(75)  # Caught up; wait under the same lease, never skip forward.
        if type(start) is not int or not 0 <= start <= head:
            raise ValueError('Window start is beyond finalized head')
        end = min(head, start + 3)
        def check_history():
            floor = rpc.call('getFirstAvailableBlock', [])
            if type(floor) is not int or not 0 <= floor <= start:
                raise ValueError('Historical window unavailable; preserve unresolved gap')
        check_history()
        leaders = rpc.call('getSlotLeaders', [start, end - start + 1])
        slots = rpc.call('getBlocks', [start, end, {'commitment': 'finalized'}])
        check_history()  # Retention can advance while the manifest is being fetched.
        if (not isinstance(leaders, list) or len(leaders) != end - start + 1
                or not isinstance(slots, list) or any(type(s) is not int for s in slots)
                or slots != sorted(set(slots)) or any(not start <= s <= end for s in slots)):
            raise ValueError('Invalid leader schedule or finalized manifest')
        for leader in leaders:
            Pubkey.from_string(leader)
        cached = None
        if output.exists():
            prior = read_json(output)
            if (prior.get('schema') == 'freya-native-window-v1' and prior.get('source_identity') == identity
                    and 0 <= time.time() - prior.get('shred_version_at', 0) <= 300
                    and type(prior.get('shred_version')) is int and 1 <= prior['shred_version'] <= 65535):
                cached = prior['shred_version']
        reuse = cached and isinstance(prior.get('bootstrap_nodes'), list) and prior['bootstrap_nodes']
        cluster = [] if reuse else rpc.call('getClusterNodes', [])
        if not isinstance(cluster, list) or len(cluster) > 20000:
            raise ValueError('Invalid cluster metadata')
        versions = Counter(row.get('shredVersion') for row in cluster if isinstance(row, dict)
                           and type(row.get('shredVersion')) is int and 1 <= row['shredVersion'] <= 65535)
        if not versions and cached is None:
            raise ValueError('Compatible shred version unavailable')
        version = cached or versions.most_common(1)[0][0]
        version_at = prior['shred_version_at'] if cached else time.time()
        peers = prior['bootstrap_nodes'] if reuse else bootstrap_nodes(cluster, version)
        plan = {'schema': 'freya-native-window-v1', 'at': time.time(), 'start': start, 'end': end,
                'observed_finalized_head': head, 'genesis': '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d',
                'source_id': rpc.source, 'source_identity': identity,
                'verification': 'reviewed_rpc_claim', 'shred_version': version,
                'shred_version_at': version_at,
                'bootstrap_nodes': peers, 'bootstrap_provenance': 'reviewed_rpc_claim',
                'slots': [{'slot': slot, 'leader': leaders[slot-start]} for slot in slots],
                'rpc_reported_skipped_slots': [slot for slot in range(start, end+1) if slot not in slots]}
        temporary = output.with_suffix('.pending')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w') as stream:
            json.dump(plan, stream, allow_nan=False); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, output)
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        close_rpc_clients([rpc])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=int)
    args = parser.parse_args()
    try:
        prepare(args.config, args.output, args.start)
    except Exception:
        raise SystemExit('Native window planning failed; checkpoint preserved (details omitted)') from None


if __name__ == '__main__':
    main()
