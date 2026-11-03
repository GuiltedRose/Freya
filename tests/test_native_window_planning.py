"""Offline manifest provenance: pruning is not evidence of skipped slots."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from prepare_native_window import prepare


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'window.json'
        self.rpc = MagicMock(source='fixture')
        self.floors = iter([5, 5])
        def call(method, params):
            if method == 'getFirstAvailableBlock':
                return next(self.floors)
            return {'getSlot': 10, 'getSlotLeaders': ['11111111111111111111111111111111']*4,
                    'getBlocks': [7, 9], 'getClusterNodes': [{'shredVersion': 7}]}[method]
        self.rpc.call.side_effect = call
        config = {'endpoints': [{'enabled': True, 'backend': 'rust', 'url': 'https://fixture.invalid'}]}
        for context in (patch('pipeline_runtime.read_json', return_value=config),
                        patch('pipeline_runtime.resolve_indexer', return_value=config),
                        patch('solana_rpc_compat.make_rpc', return_value=self.rpc),
                        patch('solana_rpc_compat.close_rpc_clients'),
                        patch('provider_limits.BUDGET.identity', return_value='private-test-identity')):
            context.start()
            self.addCleanup(context.stop)

    def test_pruned_window_is_not_published_as_skipped(self):
        self.floors = iter([8])
        with self.assertRaises(ValueError):
            prepare(Path('unused'), self.output, 7)
        self.assertFalse(self.output.exists())
        self.assertNotIn('getBlocks', [call.args[0] for call in self.rpc.call.call_args_list])

    def test_retention_race_preserves_existing_evidence(self):
        self.output.write_text('{"original":"evidence"}')
        self.floors = iter([5, 8])
        with self.assertRaises(ValueError):
            prepare(Path('unused'), self.output, 7)
        self.assertEqual(json.loads(self.output.read_text()), {'original': 'evidence'})

    def test_nonpruned_manifest_preserves_explicit_skip_provenance(self):
        prepare(Path('unused'), self.output, 7)
        record = json.loads(self.output.read_text())
        self.assertEqual(record['rpc_reported_skipped_slots'], [8, 10])
        self.assertEqual([row['slot'] for row in record['slots']], [7, 9])
        self.assertEqual(record['verification'], 'reviewed_rpc_claim')

    def test_caught_up_is_retryable_without_replacing_window(self):
        with self.assertRaises(SystemExit) as result:
            prepare(Path('unused'), self.output, 11)
        self.assertEqual(result.exception.code, 75)
        self.assertFalse(self.output.exists())
