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
                        patch('provider_limits.BUDGET.identity', return_value={'provider_id': 'a'*64, 'endpoint_id': 'b'*64})):
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
        self.assertEqual(record['source_identity'], {'provider_id': 'a'*64, 'endpoint_id': 'b'*64})

    def test_caught_up_is_retryable_without_replacing_window(self):
        with self.assertRaises(SystemExit) as result:
            prepare(Path('unused'), self.output, 11)
        self.assertEqual(result.exception.code, 75)
        self.assertFalse(self.output.exists())

    def test_current_run_progress_and_atomic_publication(self):
        prepare(Path('unused'), self.output, 7, 'current')
        progress=json.loads((self.output.parent/'planning-progress.json').read_text())
        self.assertEqual(progress['run_id'], 'current')
        self.assertEqual(progress['status'], 'completed')
        self.assertEqual([row['stage'] for row in progress['stages']], [
            'rpc_validation','finalized_head','history_floor','leader_schedule',
            'finalized_manifest','history_recheck','cluster_nodes','window_publication'])
        self.assertTrue(all(row['elapsed_seconds'] >= 0 for row in progress['stages']))
        self.assertLess(len(json.dumps(progress)), 8192)
        self.assertEqual((self.output.parent/'planning-progress.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.output.parent/'planning-progress.pending').exists())

    def test_timing_snapshot_is_bounded_atomic_and_redacted(self):
        from prepare_native_window import PlanningProgress
        progress=PlanningProgress(self.output,'timed')
        progress.begin('leader_schedule','getSlotLeaders')
        progress.observe('provider_backoff',{'reservation_elapsed_ms':30000,
            'next_wait_seconds':30,'url':'https://private.invalid/SECRET',
            'response_body_ms':float('nan')})
        record=json.loads(progress.path.read_text())
        self.assertEqual(record['rpc_timing']['phase'],'provider_backoff')
        self.assertEqual(record['rpc_timing']['reservation_elapsed_ms'],30000)
        self.assertNotIn('response_body_ms',record['rpc_timing'])
        progress.observe('http_finished',{'request_headers_ms':10,'response_body_ms':20,
            'transport_phase':'transport_complete','bytes_received':500})
        progress.complete()
        record=json.loads(progress.path.read_text())
        self.assertEqual(record['stages'][0]['rpc_timing']['response_body_ms'],20)
        self.assertNotIn('SECRET',json.dumps(record))
        self.assertLess(len(json.dumps(record)),8192)
        self.assertFalse(progress.path.with_suffix('.pending').exists())

    def test_transport_failure_stage_and_redaction(self):
        for category, phase in [('request_timeout','response_body'), ('connect_timeout','connect')]:
            with self.subTest(category=category):
                error=ConnectionError('SECRET https://private.invalid/token response body')
                error.category=category;error.phase=phase;error.elapsed_ms=30000;error.bytes_received=42
                self.rpc.validate.side_effect=error
                with self.assertRaises(ConnectionError):
                    prepare(Path('unused'), self.output, 7, category)
                record=json.loads((self.output.parent/'planning-error.json').read_text())
                self.assertEqual(record['category'], category)
                self.assertEqual(record['phase'], phase)
                self.assertEqual(record['rpc_method'], 'getGenesisHash')
                self.assertEqual(record['bytes_received'], 42)
                for name in ('planning-error.json','planning-progress.json'):
                    text=(self.output.parent/name).read_text()
                    self.assertNotIn('SECRET', text);self.assertNotIn('private.invalid', text)
                self.assertFalse(self.output.exists())

    def test_stale_error_followed_by_uncatchable_interruption(self):
        error_path=self.output.parent/'planning-error.json'
        error_path.write_text('{"run_id":"old","category":"connect_timeout"}')
        class Killed(BaseException): pass
        self.rpc.validate.side_effect=Killed()
        with self.assertRaises(Killed):
            prepare(Path('unused'), self.output, 7, 'new')
        record=json.loads(error_path.read_text())
        self.assertEqual(record['run_id'], 'new')
        self.assertEqual(record['category'], 'in_progress')
        progress=json.loads((self.output.parent/'planning-progress.json').read_text())
        self.assertEqual(progress['run_id'], 'new')
        self.assertEqual(progress['stage'], 'rpc_validation')
        self.assertEqual(progress['status'], 'running')
        self.assertFalse(self.output.exists())

    def test_provider_and_metadata_categories_are_bounded(self):
        from prepare_native_window import planning_error
        from provider_limits import ProviderBackoff
        from rpc_contract import http_error
        for error, category in [(ProviderBackoff(10),'throttled'),
                                (http_error(401,'private'),'authorization_denied'),
                                (ValueError('SECRET'),'invalid_rpc_metadata')]:
            self.assertEqual(planning_error(error)['category'], category)
            self.assertNotIn('SECRET',json.dumps(planning_error(error)))



class BootstrapSelectionTests(unittest.TestCase):
    def test_public_compatible_unique_candidates_are_bounded(self):
        from solders.pubkey import Pubkey
        from prepare_native_window import bootstrap_nodes
        rows = [{'pubkey': str(Pubkey.new_unique()), 'gossip': f'1.1.1.1:{8000+i}',
                 'shredVersion': 7} for i in range(6)]
        selected = bootstrap_nodes([rows[0], rows[0], *rows[1:]], 7)
        self.assertEqual(len(selected), 4)
        self.assertEqual(selected[0]['gossip'], '1.1.1.1:8000')
        self.assertEqual(len({row['identity'] for row in selected}), 4)

    def test_private_multicast_invalid_and_incompatible_candidates_are_rejected(self):
        from prepare_native_window import bootstrap_nodes
        valid = {'pubkey': '11111111111111111111111111111111', 'gossip': '1.1.1.1:8001',
                 'shredVersion': 7}
        bad = [dict(valid, gossip=address) for address in
               ('127.0.0.1:8001', '192.168.1.1:8001', '224.0.0.1:8001',
                'hostname.invalid:8001', '1.1.1.1:0', '1.1.1.1:65536')]
        bad += [dict(valid, pubkey='invalid'), dict(valid, shredVersion=8), None]
        self.assertEqual(bootstrap_nodes(bad, 7), [])
