import hashlib
import tempfile
import unittest
from pathlib import Path

from replay_recent_slot import stage_file, validate_receipts, validate_report


class ReplayTests(unittest.TestCase):
    def test_requires_exact_target_bank_hash(self):
        for report in ({'slot': 40, 'hash': 'expected'}, {'slot': 41, 'hash': 'wrong'}):
            with self.assertRaisesRegex(ValueError, 'expected slot and bank hash'):
                validate_report(report, 41, 'expected')
        result = validate_report({'slot': 41, 'hash': 'expected'}, 41, 'expected')
        self.assertEqual(result['finality'], 'not_independently_verified')
        self.assertFalse(result['market_data_ready'])

    def test_staging_checks_input_hash_without_replacing_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'target'
            source.write_bytes(b'fixture')
            with self.assertRaisesRegex(ValueError, 'reviewed SHA-256'):
                stage_file(source, target, '0' * 64)
            with self.assertRaises(FileExistsError):
                stage_file(source, target, hashlib.sha256(b'fixture').hexdigest())
            self.assertEqual(source.read_bytes(), b'fixture')

    def test_fallback_output_without_execution_metadata_is_rejected(self):
        block = {'parentSlot': 40, 'entries': [{'startingTransactionIndex': 0, 'numTransactions': 1,
                                                'transactions': [{'meta': None}]}]}
        with self.assertRaisesRegex(ValueError, 'without extended execution metadata'):
            validate_receipts(block, 40)
        block['entries'][0]['transactions'][0]['meta'] = {
            'err': None, 'fee': 1, 'preBalances': [], 'postBalances': [],
            'innerInstructions': [], 'logMessages': [],
        }
        self.assertEqual(validate_receipts(block, 40), 1)
