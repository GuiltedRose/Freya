"""Fresh bootstrap must never replace retained runtime or accounting files."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
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
        validator = patch('runtime_health.validate_config')
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
        with sqlite3.connect(self.ledger) as db:
            self.assertEqual(db.execute('SELECT length(secret) FROM provider_audit_key').fetchone(), (32,))
            self.assertEqual(db.execute('SELECT next_at,blocked FROM providers').fetchone(), (160,160))
            self.assertIn('prior history unavailable', db.execute('SELECT reason FROM bootstrap_provenance').fetchone()[0])
        self.assertFalse(result['historical_accounting_recovered'])

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
        with patch('runtime_health.validate_config', side_effect=ValueError('unreviewed')):
            with self.assertRaises(ValueError):
                self.run_bootstrap()
        self.assertFalse(self.config.exists())
        self.assertFalse(self.ledger.exists())
