"""Replay one reconstructed slot offline against an operator-reviewed recent parent."""
import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import stat
import subprocess
from pathlib import Path

from sync_policy import slot


def stage_file(source, target, expected_hash):
    digest = hashlib.sha256()
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as incoming:
        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
            raise ValueError('Replay input must be a regular file')
        with target.open('xb') as output:
            while data := incoming.read(4 * 1024 * 1024):
                digest.update(data)
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
    if digest.hexdigest() != expected_hash:
        raise ValueError('Staged replay input did not match its reviewed SHA-256')


def validate_report(report, target, expected_hash):
    if type(report.get('slot')) is not int or report['slot'] != target or report.get('hash') != expected_hash:
        raise ValueError('Agave did not replay the expected slot and bank hash')
    return {'slot': target, 'bank_hash': expected_hash, 'replay_matches_reviewed_hash': True,
            'finality': 'not_independently_verified', 'market_data_ready': False}


def validate_receipts(block, parent):
    if block.get('parentSlot') != parent or not isinstance(block.get('entries'), list) or not block['entries']:
        raise ValueError('Replay output does not contain the expected block entries')
    count = 0
    for entry in block['entries']:
        transactions = entry['transactions']
        if (not isinstance(transactions, list) or entry['startingTransactionIndex'] != count
                or entry['numTransactions'] != len(transactions)):
            raise ValueError('Replay receipt positions are incomplete')
        for transaction in transactions:
            meta = transaction.get('meta')
            if not isinstance(meta, dict) or not {'err', 'fee', 'preBalances', 'postBalances',
                                                   'innerInstructions', 'logMessages'}.issubset(meta):
                raise ValueError('Agave returned a transaction without extended execution metadata')
        count += len(transactions)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('anchor', 'snapshot', 'genesis', 'shreds', 'work-dir', 'node', 'ledger-tool'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--memory-gib', type=int, default=16)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    if not 4 <= args.memory_gib <= 64 or not 10 <= args.timeout <= 3600:
        parser.error('Memory must be 4–64 GiB and timeout 10–3600 seconds')

    os.umask(0o077)
    try:
        from solders.hash import Hash
        bwrap = shutil.which('bwrap')
        if not bwrap:
            raise ValueError('Bubblewrap is required for offline replay')
        anchor = json.loads(args.anchor.read_text())
        target, parent = slot(anchor['slot']), slot(anchor['parent_slot'])
        if anchor.get('schema') != 'freya-recent-replay-v1' or target <= parent:
            raise ValueError('Expected a reviewed recent parent and target slot')
        expected = str(Hash.from_string(anchor['expected_bank_hash']))
        for key in ('genesis_sha256', 'snapshot_sha256'):
            if not re.fullmatch(r'[0-9a-f]{64}', anchor[key]):
                raise ValueError('Reviewed SHA-256 digests are required')
        if not re.fullmatch(rf'snapshot-{parent}-[1-9A-HJ-NP-Za-km-z]+\.tar\.zst', args.snapshot.name):
            raise ValueError('Expected a full snapshot archive for the target slot parent')
        work = args.work_dir.resolve()
        work.mkdir(mode=0o700)
        staged_anchor = work / 'anchor.json'
        staged_anchor.write_text(json.dumps(anchor))
        sandbox = [bwrap, '--unshare-user', '--unshare-net', '--unshare-pid', '--die-with-parent',
                   '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
                   '--bind', str(work), str(work), '--chdir', str(work), '--setenv', 'TMPDIR', str(work)]
        ledger = work / 'ledger'
        snapshots = work / 'snapshots'
        snapshots.mkdir()
        stage_file(args.snapshot, snapshots / args.snapshot.name, anchor['snapshot_sha256'])

        def memory_limit():
            cap = args.memory_gib * 1024**3
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))

        with (work / 'export.log').open('xb') as log:
            subprocess.run(sandbox + [str(args.node.resolve()), '--peer', 'export-replay',
                                      str(staged_anchor), str(args.shreds.resolve()), str(ledger)],
                           stdout=log, stderr=log, check=True, timeout=args.timeout, preexec_fn=memory_limit)
        stage_file(args.genesis, ledger / 'genesis.bin', anchor['genesis_sha256'])
        with (work / 'bank.json').open('xb') as output, (work / 'replay.log').open('xb') as log:
            subprocess.run(sandbox + [str(args.ledger_tool.resolve()), '--ledger', str(ledger),
                                      '--output', 'json-compact', 'verify', '--snapshots', str(snapshots),
                                      '--accounts', str(work / 'accounts'), '--halt-at-slot', str(target),
                                      '--enable-rpc-transaction-history', '--enable-extended-tx-metadata-storage',
                                      '--print-bank-hash'],
                           stdout=output, stderr=log, check=True, timeout=args.timeout, preexec_fn=memory_limit)
        with (work / 'bank.json').open('rb') as stream:
            report = json.loads(stream.read(65537))
        result = validate_report(report, target, expected)
        pending = work / 'receipts.pending'
        with pending.open('xb') as output, (work / 'receipts.log').open('xb') as log:
            subprocess.run(sandbox + [str(args.ledger_tool.resolve()), '--ledger', str(ledger),
                                      '--output', 'json-compact', '-vv', 'slot', str(target)],
                           stdout=output, stderr=log, check=True, timeout=args.timeout, preexec_fn=memory_limit)
        with pending.open('rb') as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('Extended replay receipts exceed the result size limit')
        result['receipt_count'] = validate_receipts(json.loads(raw), parent)
        pending.rename(work / 'receipts.json')
        print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Replay stopped. Review the private replay work directory. {type(error).__name__}\n')


if __name__ == '__main__':
    main()
