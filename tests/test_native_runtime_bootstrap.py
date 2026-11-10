"""Fresh bootstrap must never replace retained runtime or accounting files."""
from contextlib import closing
import json
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prepare_native_runtime import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'reviewed.json'
        self.document = {'endpoints': [{'enabled': True, 'backend': 'rust',
                                       'url': 'https://fixture.invalid',
                                       'policy': {'execution_verified': False}}]}
        self.source.write_text(json.dumps(self.document))
        self.config = self.root/'private/config.json'
        self.ledger = self.root/'state/provider_limits.sqlite'
        validator = patch('prepare_mainnet_host.validate_native_source',return_value=self.document['endpoints'][0])
        validator.start()
        self.addCleanup(validator.stop)

    def run_bootstrap(self):
        return bootstrap(self.source, self.config, self.ledger, acknowledge_loss=True)

    def test_new_epoch_is_private_delayed_and_preserves_policy(self):
        with patch('prepare_native_runtime.time.time', return_value=100):
            result = self.run_bootstrap()
        self.assertEqual(json.loads(self.config.read_text()), self.document)
        for path in (self.config, self.ledger):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute('SELECT length(secret) FROM provider_audit_key').fetchone(), (32,))
            self.assertEqual(db.execute('SELECT next_at,blocked FROM providers').fetchone(), (160,160))
            self.assertIn('prior history unavailable', db.execute('SELECT reason FROM bootstrap_provenance').fetchone()[0])
        self.assertFalse(result['historical_accounting_recovered'])
        self.assertFalse(result['decoder_qualification_granted'])
        self.assertEqual(result['scope'],'native_rpc_planning_only')

    def test_missing_acknowledgement_changes_nothing(self):
        with self.assertRaises(ValueError):
            bootstrap(self.source, self.config, self.ledger)
        self.assertFalse(self.config.parent.exists())
        self.assertFalse(self.ledger.parent.exists())

    def test_existing_files_and_dangling_symlinks_are_never_replaced(self):
        self.ledger.parent.mkdir()
        self.ledger.write_bytes(b'retained accounting')
        with self.assertRaises(ValueError):
            self.run_bootstrap()
        self.assertEqual(self.ledger.read_bytes(), b'retained accounting')
        self.assertFalse(self.config.exists())
        self.ledger.unlink()
        self.ledger.symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):
            self.run_bootstrap()
        self.assertTrue(self.ledger.is_symlink())
        self.assertFalse(self.config.exists())

    def test_invalid_reviewed_source_publishes_nothing(self):
        with patch('prepare_mainnet_host.validate_native_source', side_effect=ValueError('unreviewed source')):
            with self.assertRaises(ValueError):
                self.run_bootstrap()
        self.assertFalse(self.config.exists())
        self.assertFalse(self.ledger.exists())

    def test_native_setup_does_not_grant_or_rewrite_stale_decoder_review(self):
        self.document['reviewed_program_versions']={'fixture':[{'idl_hash':'retired-schema'}]}
        self.source.write_text(json.dumps(self.document))
        with patch('runtime_health.validate_config',side_effect=ValueError('stale decoder')) as full_validator:
            result=self.run_bootstrap()
        full_validator.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text()),self.document)
        self.assertFalse(result['decoder_qualification_granted'])


class NativeSourceTests(unittest.TestCase):
    def test_source_policy_and_native_transport_still_gate_setup(self):
        from prepare_mainnet_host import validate_native_source, PreparationError
        endpoint={'enabled':True,'backend':'rust','url':'https://fixture.invalid'}
        with patch('solana_rpc_compat.check_boundary') as boundary,patch('solana_rpc_compat.native_module') as native:
            self.assertIs(validate_native_source({'endpoints':[endpoint]}),endpoint)
            boundary.assert_called_once_with(endpoint)
            native.assert_called_once_with()
        with patch('solana_rpc_compat.check_boundary',side_effect=ValueError('policy missing')):
            with self.assertRaises(ValueError):validate_native_source({'endpoints':[endpoint]})
        with self.assertRaises(PreparationError):validate_native_source({'endpoints':[]})

    def test_shipped_source_can_plan_but_full_ingestion_rejects_stale_pins(self):
        from prepare_mainnet_host import validate_native_source
        from runtime_health import validate_config
        source=json.loads((Path(__file__).parents[1]/'deployment/native-mainnet.reviewed.json').read_text())
        with patch('solana_rpc_compat.native_module'),patch('solana_rpc_compat.check_boundary'):
            self.assertEqual(validate_native_source(source)['backend'],'rust')
            with self.assertRaisesRegex(ValueError,'Pinned SDK schema changed'):
                validate_config(source)

    def test_fresh_host_preparation_preserves_source_mode_and_caps(self):
        from prepare_mainnet_host import prepare
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/'source.json';source.write_text('{}');source.chmod(0o600)
            ledger=root/'provider.sqlite';ledger.write_bytes(b'fixture');ledger.chmod(0o600)
            endpoint={'enabled':True,'backend':'rust','network_mode':'remote_provider'}
            args=SimpleNamespace(output=root/'prepared',config=source,index_db=root/'index.sqlite',refresh=False)
            original_is_file=Path.is_file
            def is_file(path):
                return True if str(path)=='/etc/nixos/configuration.nix' else original_is_file(path)
            def capture(*command):
                if command[0]=='systemctl':return 'not-found'
                if command[:2]==('nix','build'):return '/nix/store/fixture-node'
                return json.dumps({'path':'/nix/store/fixture-source'})
            with patch.object(Path,'is_file',is_file),patch('prepare_mainnet_host.provider_limits_path',return_value=ledger),\
                 patch('prepare_mainnet_host.validate_native_source',return_value=endpoint),\
                 patch('prepare_mainnet_host.default_uplink',return_value='eth0'),\
                 patch('prepare_mainnet_host.capture',side_effect=capture),patch('sys.stdout',new_callable=io.StringIO):
                prepare(args)
            self.assertEqual(json.loads((args.output/'node.json').read_text())['network_mode'],'remote_provider')
            module=(args.output/'freya-mainnet.nix').read_text()
            self.assertIn('sessionBytes = 1000000;',module)
            self.assertIn('sessionSeconds = 55;',module)
            self.assertIn('networkMode = "isolated";',module)
