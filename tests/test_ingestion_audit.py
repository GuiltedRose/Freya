"""Read-only, resumable historical audit; temporary fixture databases only."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from audit_ingestion import audit
from onchain_decode import Decoder
from onchain_store import EventStore
from test_onchain_indexer import transaction,block,POLICY


@unittest.skipUnless(importlib.util.find_spec('_solana_binary'),'Native event replay required')
class IngestionAuditTests(unittest.TestCase):
    def create(self,directory):
        path=Path(directory)/'index.sqlite';store=EventStore(path);store.register_source('fixture',POLICY)
        for slot in (1,2):
            tx=transaction(signature='fixture-'+str(slot))
            events,issues=Decoder().transaction(tx,slot,0,slot*60)
            store.commit_range(slot,slot,[slot],[(slot,block(slot,slot*60,[tx]),events,issues,[(0,tx)])],'fixture')
        return path,store

    def test_pages_resume_without_mutating_receipts_or_promoting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path,store=self.create(directory)
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            first=audit(path,1)
            second=audit(path,1,first['after']['slot'],first['after']['tx_index'])
            self.assertEqual((first['receipts'],first['clean'],second['receipts'],second['clean']),(1,1,1,1))
            self.assertEqual(second['after']['slot'],2)
            self.assertTrue(first['parity'])
            self.assertFalse(first['promotion_authorized'])
            self.assertFalse(first['source_admission_checked'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_conflicting_hash_is_reported_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path,store=self.create(directory)
            with store.connect() as db:db.execute('UPDATE transactions SET raw_hash=? WHERE slot=1',('invalid',))
            report=audit(path,2)
            self.assertEqual(report['clean'],1)
            self.assertTrue(report['errors'])
            with store.connect() as db:self.assertEqual(db.execute('SELECT raw_hash FROM transactions WHERE slot=1').fetchone()[0],'invalid')
