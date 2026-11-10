"""Offline historical-prefix tests. Fixtures do not demonstrate live availability."""
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from onchain_ingest import Ingester
from onchain_store import EventStore
from rpc_contract import RpcHistoryUnavailable, remote_error
from sync_policy import initial_slot, initialize_store, history_available


def block(slot, parent, stamp=100):
    return {'parentSlot':parent,'previousBlockhash':str(parent),'blockhash':str(slot),
            'blockTime':stamp,'transactions':[]}


class HistoryRpc:
    source='fixture'
    def __init__(self, blocks, head=8, floor=1):
        self.blocks=blocks
        self.head=head
        self.floor=floor
        self.calls=[]
    def validate(self):
        pass
    def call(self, method, params):
        self.calls.append((method,params))
        if method=='getSlot':return self.head
        if method=='getFirstAvailableBlock':return self.floor
        if method=='getBlocks':return sorted(s for s in self.blocks if params[0]<=s<=params[1])
        if method=='getBlock':return self.blocks.get(params[0])
        raise AssertionError(method)


class GenesisTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'index.sqlite'
        self.store=EventStore(self.path,retain_blocks=True)
        self.store.register_source('fixture',{'storage_verified':True,'evidence':'offline fixture'})

    def ingester(self, rpc):
        decoder=SimpleNamespace(specs={},transaction=lambda *args:([],[]))
        return Ingester(self.store,[rpc],decoder=decoder)

    def test_authoritative_start_and_valid_checkpoint_precedence(self):
        self.assertEqual(initial_slot(),0)
        self.assertEqual(initial_slot(17,99),17)
        self.assertEqual(initial_slot(requested=7),7)
        for value in (True,-1,'0',2**63):
            with self.assertRaises(ValueError):initial_slot(value)

    def test_fresh_boundary_is_requested_not_successfully_processed(self):
        self.assertEqual(initialize_store(self.store),0)
        self.assertEqual(self.store.metrics()['blocks'],0)
        self.assertEqual(self.store.metrics()['ranges'],0)
        self.assertIsNone(initialize_store(self.store,100))
        self.assertEqual(self.store.next_slot(),0)

    def test_genesis_is_boundary_and_does_not_require_getblock_zero(self):
        rpc=HistoryRpc({1:block(1,0)},head=1)
        self.ingester(rpc).step(count=2)
        self.assertEqual(self.store.next_slot(),2)
        self.assertEqual(self.store.metrics()['blocks'],1)
        self.assertFalse(any(m=='getBlock' and p[0]==0 for m,p in rpc.calls))
        self.assertTrue(history_available(0,1))
        self.assertFalse(history_available(0,2))

    def test_empty_manifest_persists_gap_without_advancing_then_recovers(self):
        rpc=HistoryRpc({7:block(7,0)},head=8)
        ingester=self.ingester(rpc)
        result=ingester.step(count=4)
        self.assertEqual(result['status'],'unresolved_history')
        self.assertEqual(self.store.next_slot(),0)
        self.assertEqual(self.store.metrics()['ranges'],0)
        # Restart uses discovery progress, not a false committed checkpoint.
        self.store=EventStore(self.path,retain_blocks=True)
        self.ingester(rpc).step(count=4)
        self.assertEqual(self.store.next_slot(),8)
        self.assertIn(('getBlocks',[4,7,{'commitment':'finalized'}]),rpc.calls)

    def test_skipped_slot_needs_successor_parent_and_hash_continuity(self):
        rpc=HistoryRpc({1:block(1,0),3:block(3,1)},head=3)
        self.ingester(rpc).step(count=4)
        self.assertEqual(self.store.next_slot(),4)
        self.assertEqual(self.store.metrics()['blocks'],2)

    def test_omitted_produced_slot_never_becomes_successful_gap(self):
        rpc=HistoryRpc({1:block(1,0),3:block(3,2)},head=3)
        with self.assertRaises(ValueError):self.ingester(rpc).step(count=4)
        self.assertEqual(self.store.next_slot(),2)
        self.assertEqual(self.store.metrics()['blocks'],1)

    def test_pruned_provider_fails_before_manifest_without_cursor_jump(self):
        rpc=HistoryRpc({},floor=100)
        with self.assertRaises(RpcHistoryUnavailable):self.ingester(rpc).step()
        self.assertEqual(self.store.next_slot(),0)
        self.assertFalse(any(m=='getBlocks' for m,_ in rpc.calls))

    def test_earliest_block_parent_can_prove_genesis_skips_without_pruning(self):
        rpc=HistoryRpc({7:block(7,0)},head=7,floor=7)
        self.ingester(rpc).step(count=4)
        self.assertEqual(self.store.next_slot(),8)
        self.assertEqual(self.store.metrics()['blocks'],1)

    def test_earliest_block_with_missing_produced_parent_is_not_genesis(self):
        rpc=HistoryRpc({7:block(7,6)},head=7,floor=7)
        with self.assertRaises(RpcHistoryUnavailable):self.ingester(rpc).step(count=4)
        self.assertEqual(self.store.next_slot(),0)

    def test_explicit_backup_has_different_floor_and_recovers_original_cursor(self):
        primary=HistoryRpc({7:block(7,6)},head=7,floor=7)
        backup=HistoryRpc({1:block(1,0)},head=1,floor=1)
        backup.source='archive'
        self.store.register_source('archive',{'storage_verified':True,'evidence':'offline fixture'})
        engine=self.ingester(primary)
        engine.endpoints.append(backup)
        engine.step(count=2)
        self.assertEqual(self.store.next_slot(),2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT source FROM blocks').fetchone()[0],'archive')

    def test_null_historical_time_is_retained_without_fabricated_events(self):
        self.ingester(HistoryRpc({1:block(1,0,None)},head=1)).step(count=2)
        with self.store.connect() as db:
            row=db.execute('SELECT time,issues FROM blocks WHERE slot=1').fetchone()
        self.assertIsNone(row['time'])
        self.assertIn('time unavailable',row['issues'])
        self.assertEqual(self.store.metrics()['events'],0)
        self.assertEqual(self.store.next_slot(),2)

    def test_unknown_time_does_not_disable_existing_monotonicity_veto(self):
        rpc=HistoryRpc({1:block(1,0,100),2:block(2,1,None),3:block(3,2,99)},head=3)
        with self.assertRaises(ValueError):self.ingester(rpc).step(count=4)
        self.assertEqual(self.store.next_slot(),3)
        self.assertEqual(self.store.metrics()['blocks'],2)

    def test_unanchored_store_range_and_out_of_order_commit_are_rejected(self):
        initialize_store(self.store)
        with self.assertRaises(ValueError):self.store.commit_range(0,7,[],[],'fixture')
        b=block(9,8)
        with self.assertRaises(ValueError):self.store.commit_range(9,9,[9],[(9,b,[],[],[])],'fixture')
        self.assertEqual(self.store.next_slot(),0)

    def test_legacy_skip_only_checkpoint_is_preserved_but_not_trusted(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO ranges VALUES(0,7,'fixture','[]')")
            db.execute("INSERT INTO checkpoints VALUES('next_slot',8)")
        with self.assertRaises(ValueError):initialize_store(self.store)
        with self.assertRaises(ValueError):self.ingester(HistoryRpc({})).step()
        self.assertEqual(self.store.next_slot(),8)
        self.assertEqual(self.store.metrics()['ranges'],1)
        self.assertIsNone(self.store.metrics()['committed_next_slot'])

    def test_missing_block_and_repair_failure_preserve_durable_prefix(self):
        rpc=HistoryRpc({1:block(1,0),2:block(2,1)},head=2)
        original=rpc.call
        def fail(method,params):
            if method=='getBlock' and params[0]==2:raise RpcHistoryUnavailable('fixture')
            return original(method,params)
        rpc.call=fail
        with self.assertRaises(RpcHistoryUnavailable):self.ingester(rpc).step(count=3)
        self.assertEqual(self.store.next_slot(),2)
        rpc.call=original
        self.ingester(rpc).step(count=3)
        self.assertEqual(self.store.next_slot(),3)

    def test_ambiguous_snapshot_and_long_term_errors_are_not_skips(self):
        for code in (-32007,-32009,-32019):
            self.assertIsInstance(remote_error(code,'fixture'),RpcHistoryUnavailable)

    def test_existing_nonnullable_schema_migrates_without_losing_history(self):
        legacy=Path(self.temporary.name)/'legacy.sqlite'
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE blocks(slot INTEGER PRIMARY KEY,hash TEXT NOT NULL,parent INTEGER NOT NULL,time INTEGER NOT NULL,source TEXT NOT NULL,issues TEXT NOT NULL,raw_hash TEXT NOT NULL,transactions INTEGER NOT NULL)')
            db.execute("INSERT INTO blocks VALUES(1,'hash',0,100,'fixture','[]','digest',0)")
            db.execute('CREATE INDEX retained_custom_index ON blocks(hash)')
        migrated=EventStore(legacy)
        with migrated.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM blocks').fetchone()),(1,'hash',0,100,'fixture','[]','digest',0))
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='retained_custom_index'").fetchone())
        with self.assertRaises(ValueError):initialize_store(migrated)
