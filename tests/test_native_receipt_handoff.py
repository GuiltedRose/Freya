"""Focused receipt strategy checks; fixtures do not demonstrate mainnet readiness."""
import json
from pathlib import Path
import tempfile
import unittest
from native_handoff import reconcile


class ReceiptHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / '42'
        self.folder.mkdir()
        self.record = {'schema': 'freya-native-slot-v1', 'slot': 42, 'parent_slot': 41,
                       'leader': 'fixture-leader', 'reconstructed': True, 'authoritative': False,
                       'conflict': False, 'missing_indices': [], 'terminal_index': 2,
                       'execution_metadata': None, 'finality': 'unverified',
                       'verification': 'operator_configured_leader_signature',
                       'transaction_signatures': ['first', 'second'],
                       'ordered_transactions': [{}, {}]}
        self.block = {'parentSlot': 41, 'transactions': [
            {'transaction': {'signatures': [s]}, 'meta': {'err': None, 'fee': 5000,
                'preBalances': [], 'postBalances': [], 'innerInstructions': [],
                'logMessages': [], 'preTokenBalances': [], 'postTokenBalances': []}}
            for s in ['first', 'second']]}
        self.save()

    def save(self):
        (self.folder / 'handoff.json').write_text(json.dumps(self.record))

    def test_native_messages_cannot_replace_execution_receipts(self):
        self.block['transactions'][0]['meta'] = None
        with self.assertRaises(ValueError):
            reconcile(self.root, 42, self.block)

    def test_matching_finalized_receipts_retain_external_authority(self):
        result = reconcile(self.root, 42, self.block)
        self.assertEqual(result['receipt_authority'], 'configured_finalized_rpc')
        self.assertFalse(result['independent_replay_verified'])
        self.assertIsNone(self.block['transactions'][0]['meta']['err'])
        self.assertEqual(self.block['transactions'][0]['meta']['fee'], 5000)

    def test_transaction_order_conflict_is_rejected(self):
        self.block['transactions'].reverse()
        with self.assertRaises(ValueError):
            reconcile(self.root, 42, self.block)

    def test_parent_conflict_is_rejected(self):
        self.block['parentSlot'] = 40
        with self.assertRaises(ValueError):
            reconcile(self.root, 42, self.block)

    def test_incomplete_or_conflicting_native_data_is_rejected(self):
        self.record['missing_indices'] = [1]
        self.save()
        with self.assertRaises(ValueError):
            reconcile(self.root, 42, self.block)
        self.record['missing_indices'] = []
        self.save()
        (self.folder / 'conflict.json').write_text('{}')
        with self.assertRaises(ValueError):
            reconcile(self.root, 42, self.block)

    def test_symlink_handoff_is_rejected(self):
        path = self.folder / 'handoff.json'
        path.rename(self.folder / 'other.json')
        path.symlink_to(self.folder / 'other.json')
        with self.assertRaises(ValueError):
            reconcile(self.root, 42, self.block)


if __name__ == '__main__':
    unittest.main()
