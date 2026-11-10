"""Offline startup regressions; temporary stores and mocked configured RPC."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from data_policy import SourceAccessBlocked
from onchain_store import EventStore
from solana_observer import observe


class StartSlotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'observer.sqlite'
        self.store=None
        self.rpc=MagicMock();self.rpc.call.return_value=123
        self.stream=AsyncMock()
        def prepare(*args):
            self.store=EventStore(self.path)
            return SimpleNamespace(store=self.store,endpoints=[self.rpc]),{'backend':'rust'},object()
        self.prepare=patch('solana_observer.prepare',side_effect=prepare)
        self.prepare.start();self.addCleanup(self.prepare.stop)
        self.runner=patch('solana_observer.run_stream',self.stream)
        self.runner.start();self.addCleanup(self.runner.stop)

    def run_observer(self,**kwargs):
        asyncio.run(observe({},self.path,max_cycles=1,**kwargs))

    def test_new_database_initializes_genesis_once_without_tip_lookup(self):
        self.run_observer()
        self.rpc.validate.assert_not_called()
        self.rpc.call.assert_not_called()
        self.assertEqual(self.store.next_slot(),0)
        self.assertEqual(self.stream.await_args.args[2],0)
        self.run_observer()
        self.assertEqual(self.store.next_slot(),0)
        self.assertIsNone(self.stream.await_args.args[2])

    def test_existing_checkpoint_wins_over_explicit_override(self):
        self.store=EventStore(self.path)
        with self.store.connect() as db:db.execute("INSERT INTO checkpoints VALUES('next_slot',77)")
        self.run_observer(start_slot=999)
        self.assertEqual(self.store.next_slot(),77)
        self.rpc.call.assert_not_called()
        self.assertIsNone(self.stream.await_args.args[2])

    def test_explicit_override_avoids_tip_lookup(self):
        self.run_observer(start_slot=0)
        self.assertEqual(self.store.next_slot(),0)
        self.rpc.call.assert_not_called()
        self.assertEqual(self.stream.await_args.args[2],0)

    def test_invalid_explicit_boundaries_never_initialize_or_start_stream(self):
        for value in (True,-1,'123',1.5,2**63):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):self.run_observer(start_slot=value)
                if self.store is not None:self.assertIsNone(self.store.next_slot())
        self.stream.assert_not_awaited()

    def test_restart_after_stream_failure_does_not_rediscover_or_skip(self):
        self.stream.side_effect=ConnectionError('interrupted after initialization')
        with self.assertRaises(ConnectionError):self.run_observer()
        self.assertEqual(self.store.next_slot(),0)
        self.stream.side_effect=None;self.rpc.call.return_value=999
        self.run_observer()
        self.assertEqual(self.store.next_slot(),0)
        self.rpc.call.assert_not_called()

    def test_historical_data_without_checkpoint_requires_recovery(self):
        self.store=EventStore(self.path)
        with self.store.connect() as db:
            db.execute("INSERT INTO ranges VALUES(7,7,'fixture','[]')")
        with self.assertRaises(ValueError):self.run_observer()
        self.assertIsNone(self.store.next_slot())
        self.stream.assert_not_awaited()
