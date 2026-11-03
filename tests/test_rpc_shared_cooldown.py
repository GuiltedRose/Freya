"""No network: cross-consumer cooldown, privacy and automatic mainnet pacing."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from provider_limits import ProviderBudget,ProviderBackoff
from onchain_ingest import IndexRpc
from rpc_run_evidence import ledger_report


class SharedCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'provider.sqlite';self.now=1000.
        self.url='https://api.mainnet-beta.solana.com/SECRET-key'
        self.budget=ProviderBudget(self.path,clock=lambda:self.now,sleep=self.advance)

    def advance(self,seconds):self.now+=seconds

    def test_queued_reservation_rechecks_new_throttle(self):
        self.budget.reserve_rpc(self.url)
        def wait(seconds):
            self.advance(seconds)
            other=ProviderBudget(self.path,clock=lambda:self.now)
            other.failure(self.url,429,{'Retry-After':'90'})
        self.budget.sleep=wait
        with self.assertRaises(ProviderBackoff) as caught:self.budget.reserve_rpc(self.url)
        self.assertEqual(caught.exception.seconds,150)
        self.assertEqual(self.budget.cooldown_remaining(self.url),150)

    def test_throttle_padding_does_not_reduce_longer_existing_block(self):
        self.budget.failure(self.url,429,{'Retry-After':'300'})
        self.assertEqual(self.budget.cooldown_remaining(self.url),360)
        self.budget.failure(self.url,429,{'Retry-After':'1'})
        self.assertEqual(self.budget.cooldown_remaining(self.url),360)
        self.budget.success(self.url)
        self.assertEqual(self.budget.cooldown_remaining(self.url),360)

    def test_http_throttle_and_internal_backoff_are_distinct_and_private(self):
        self.budget.failure(self.url,503)
        self.budget.failure(self.url,429,{'Retry-After':'90'})
        report=ledger_report(self.path,0)
        rows=report['recent_backoff']
        self.assertEqual(rows[0]['http_status'],429)
        self.assertEqual(rows[0]['origin'],'http')
        self.assertEqual(rows[0]['retry_after_seconds'],90)
        self.assertEqual(rows[0]['blocked_until'],1150)
        self.assertIsNone(rows[1]['http_status'])
        self.assertEqual(rows[1]['origin'],'internal_or_rpc')
        self.assertNotIn('SECRET',json.dumps(report))
        self.assertEqual(self.budget.identity(self.url),self.budget.identity(self.url))
        self.assertNotEqual(self.budget.identity(self.url)['endpoint_id'],self.budget.identity(self.url+'2')['endpoint_id'])

    def test_external_mainnet_defaults_cannot_be_disabled_by_isolated_label(self):
        for mode in (None,'isolated','remote_provider'):
            endpoint={'url':self.url,'source_id':'fixture','burst_cooldown_seconds':10}
            if mode:endpoint['network_mode']=mode
            rpc=IndexRpc(endpoint,self.budget)
            self.assertEqual(rpc.burst_requests,10)
            self.assertEqual(rpc.burst_cooldown_seconds,30)
        self.assertEqual(self.budget.key('https://API.MAINNET.SOLANA.COM'),self.budget.key(self.url))

    def test_normal_rest_is_thirty_seconds_without_waiting_for_429(self):
        for _ in range(10):self.budget.reserve_rpc(self.url)
        self.assertEqual(self.budget.cooldown_remaining(self.url),30)
        with self.assertRaises(ProviderBackoff):self.budget.reserve_rpc(self.url)

    def test_readonly_report_does_not_create_missing_ledger(self):
        missing=Path(self.temp.name)/'absent.sqlite'
        self.assertFalse(ledger_report(missing,0)['available'])
        self.assertFalse(missing.exists())

    def test_queued_reservation_rechecks_access_denial(self):
        from data_policy import SourceAccessBlocked
        self.budget.reserve_rpc(self.url)
        def wait(seconds):
            self.advance(seconds)
            ProviderBudget(self.path,clock=lambda:self.now).failure(self.url,403)
        self.budget.sleep=wait
        with self.assertRaises(SourceAccessBlocked):self.budget.reserve_rpc(self.url)

    def test_partial_progress_is_explicitly_not_a_current_head(self):
        from pipeline_console import format_pipeline
        text=format_pipeline({'status':'warming_up','progress':{
            'indexed_slots_per_second':.37,'lag_slots':4,'head_sample_age_seconds':80,'lag_is_current':False}})
        self.assertIn('last sampled lag 4 slots',text)
        self.assertIn('head sample 80s old',text)
        self.assertIn('(last step)',text)

    def test_audit_failure_cannot_undo_received_throttle(self):
        from unittest.mock import patch
        with patch.object(self.budget,'_audit',side_effect=sqlite3.OperationalError('audit unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                self.budget.failure(self.url,429,{'Retry-After':'90'})
        self.assertEqual(self.budget.cooldown_remaining(self.url),150)

    def test_readonly_request_waits_without_restarting_metadata_or_sending_early(self):
        from unittest.mock import patch
        rpc=IndexRpc({'url':self.url,'source_id':'fixture'},self.budget)
        for _ in range(10):self.budget.reserve_rpc(self.url)
        start=self.now
        with patch('onchain_ingest.build_opener') as opener:
            response=opener.return_value.open.return_value.__enter__.return_value
            response.status=200;response.headers={}
            response.read.return_value=b'{"jsonrpc":"2.0","id":1,"result":{"transactions":[]}}'
            self.assertEqual(rpc.call('getBlock',[1,{}]),{'transactions':[]})
            opener.return_value.open.assert_called_once()
        self.assertEqual(self.now-start,30)
        db=self.budget.connect()
        try:waits=db.execute("SELECT method,http_status FROM provider_events WHERE event='wait'").fetchall()
        finally:db.close()
        self.assertEqual(waits,[('getBlock',None)])
        self.assertEqual(rpc.requests,1)

    def test_observed_head_is_saved_even_when_first_block_is_throttled(self):
        from unittest.mock import patch
        from test_onchain_indexer import FakeRpc,POLICY
        from onchain_store import EventStore
        from onchain_ingest import Ingester
        from rpc_contract import http_error
        store=EventStore(Path(self.temp.name)/'index.sqlite');store.register_source('fixture',POLICY)
        rpc=FakeRpc();original=rpc.call
        def call(method,params):
            if method=='getBlock':raise http_error(429,'fixture',method)
            return original(method,params)
        rpc.call=call
        ingester=Ingester(store,[rpc])
        with patch.object(ingester,'deployments',return_value={program:0 for program in ingester.decoder.specs}):
            with self.assertRaises(ValueError):ingester.step(1,2)
        progress=store.progress()
        self.assertEqual(progress['finalized_head'],2)
        self.assertEqual(progress['next_slot'],1)
        self.assertEqual(progress['lag_slots'],2)
        self.assertEqual(store.metrics()['blocks'],0)
