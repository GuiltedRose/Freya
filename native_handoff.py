"""Read-only v1 native evidence reconciliation; RPC remains the receipt authority."""
import hashlib
import json
import os
from pathlib import Path

MAX_HANDOFF_BYTES = 32 * 1024 * 1024


def reconcile(directory, slot, block):
    if type(slot) is not int or slot < 0:
        raise ValueError('Invalid native slot')
    root = Path(directory)
    folder = root / str(slot)
    if not root.is_absolute() or root.is_symlink() or folder.is_symlink():
        raise ValueError('Native evidence requires an explicit non-symlink directory')
    if (folder / 'conflict.json').exists():
        raise ValueError('Conflicting native slot remains quarantined')
    path = folder / 'handoff.json'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            import stat
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HANDOFF_BYTES:
                raise ValueError('Invalid native handoff size/type')
            raw = stream.read(MAX_HANDOFF_BYTES + 1)
        if len(raw) > MAX_HANDOFF_BYTES:
            raise ValueError('Native handoff exceeds budget')
        record = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError('Native slot evidence unavailable or malformed') from error
    if not isinstance(record, dict) or record.get('schema') != 'freya-native-slot-v1':
        raise ValueError('Unsupported native handoff schema')
    if (record.get('slot') != slot or record.get('reconstructed') is not True
            or record.get('authoritative') is not False or record.get('conflict') is not False
            or record.get('missing_indices') != [] or type(record.get('terminal_index')) is not int
            or record.get('execution_metadata') is not None or record.get('finality') != 'unverified'
            or record.get('verification') != 'operator_configured_leader_signature'):
        raise ValueError('Incomplete or unsupported native evidence')
    signatures = record.get('transaction_signatures')
    transactions = record.get('ordered_transactions')
    if (not isinstance(signatures, list) or not all(isinstance(s, str) and s for s in signatures)
            or len(set(signatures)) != len(signatures) or not isinstance(transactions, list)
            or len(transactions) != len(signatures)):
        raise ValueError('Malformed native transaction ordering')
    try:
        receipts = block['transactions']
        actual = [tx['transaction']['signatures'][0] for tx in receipts]
        required = {'err', 'fee', 'preBalances', 'postBalances', 'innerInstructions',
                    'logMessages', 'preTokenBalances', 'postTokenBalances'}
        if any(not isinstance(tx['meta'], dict) or not required.issubset(tx['meta'])
               or (tx.get('version') == 0 and not isinstance(tx['meta'].get('loadedAddresses'), dict))
               for tx in receipts):
            raise ValueError('Execution receipt unavailable')
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError('Malformed external execution receipts') from error
    if signatures != actual:
        raise ValueError('Native and finalized RPC transaction order conflict')
    if record.get('parent_slot') != block.get('parentSlot'):
        raise ValueError('Native and finalized RPC parent conflict')
    if (folder / 'conflict.json').exists():
        raise ValueError('Native conflict appeared during reconciliation')
    return {'schema': 'freya-native-reconciliation-v1', 'slot': slot,
            'native_sha256': hashlib.sha256(raw).hexdigest(), 'leader': record.get('leader'),
            'matched': 'ordered_transaction_signatures_and_parent',
            'receipt_authority': 'configured_finalized_rpc', 'independent_replay_verified': False}
