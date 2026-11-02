"""Runtime repair contracts: mocked RPC only; no funded wallet or node required."""
import base64
import gzip
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from data_policy import SourceAccessBlocked
from solana_rpc_compat import RustIndexRpc
from onchain_ingest import IndexRpc
from pipeline_runtime import resolve_indexer
from position_recovery import RecoverySources
from wallet_signals import IndexedWalletFeed
import test_onchain_indexer as fixtures


class CompressionTests(unittest.TestCase):
    def test_warmup_never_increases_without_supported_kelly_evidence(self):
        import sqlite3
        from capital_policy import CapitalConfig,CapitalPolicy,canonical
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        policy=CapitalPolicy(db,CapitalConfig(),paper=True)
        state=policy.state();state['allocation']['tier']=policy.config.allocation_max
        with db:db.execute('UPDATE capital_policy SET state=? WHERE id=?',(canonical(state),policy.policy_id))
        decision=policy.size(100,4,0,0,liquidity_limit=100,now=100)
        self.assertEqual(decision['allocation_usd'],1)
        self.assertEqual(decision['kelly_ceiling_usd'],0)
        self.assertEqual(decision['tiers']['concurrency']['tier'],policy.config.concurrency_min)

    def test_nix_validation_cannot_copy_ignored_state_or_launch_vm_node_checks(self):
        from validate_production import nix_validation_command
        command=nix_validation_command('/tmp/public-bot-source','x86_64-linux')
        self.assertIn('git+file:///tmp/public-bot-source#native-rpc-app',command)
        self.assertIn('git+file:///tmp/public-bot-source#checks.x86_64-linux.indexer',command)
        self.assertIn('--no-write-lock-file',command)
        self.assertFalse(any(value.startswith('path:') or 'agave' in value or 'lifecycle' in value for value in command))

    def test_bounded_gzip_contract_preserves_evidence_and_byte_counts(self):
        budget=MagicMock();rpc=RustIndexRpc({'url':'https://fixture.invalid','source_id':'fixture'},budget)
        raw=json.dumps({'jsonrpc':'2.0','id':1,'result':123}).encode()
        compressed=gzip.compress(raw)
        encoded=base64.b64encode(compressed).decode()
        self.assertEqual(rpc._finish_encoded(200,'','gzip',encoded,method='getSlot'),123)
        self.assertEqual(rpc.wire_bytes,len(compressed))
        self.assertEqual(rpc.decoded_bytes,len(raw))
        for broken in (compressed+b'junk',compressed[:-1],gzip.compress(raw)+gzip.compress(raw)):
            with self.assertRaises(SourceAccessBlocked):
                rpc._finish_encoded(200,'','gzip',base64.b64encode(broken).decode(),method='getSlot')
        with self.assertRaises(SourceAccessBlocked):rpc._finish_encoded(200,'','br','anything',method='getSlot')

    def test_spacing_does_not_confuse_cycle_sleep_or_weaken_custom_policy(self):
        endpoint={'source_id':'builtin-public-mainnet-v1','url':'https://api.mainnet.solana.com',
            'interval_seconds':5,'policy':{'profile':{'kind':'public_mainnet'}}}
        original={'endpoints':[endpoint]}
        resolved=resolve_indexer(original,{})['endpoints'][0]
        self.assertEqual(IndexRpc(resolved).interval,.5)
        self.assertNotIn('request_interval_seconds',endpoint)
        custom=dict(endpoint,source_id='custom')
        self.assertEqual(IndexRpc(resolve_indexer({'endpoints':[custom]}, {})['endpoints'][0]).interval,5)
        with self.assertRaises(ValueError):IndexRpc(dict(endpoint,request_interval_seconds=float('nan')))


class RecoveryTests(unittest.TestCase):
    def test_existing_position_recovery_needs_no_index_candles_and_cannot_be_entry_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'source.json'
            config.write_text(json.dumps({'endpoints':[{'enabled':True,'source_id':'rpc',
                'url':'https://fixture.invalid','policy':{'usd_conversion_verified':True,
                'oracle_data_rights_verified':True,'oracle_max_confidence_bps':100}}],
                'oracle':{'source_id':'rpc','feeds':[{'mint':fixtures.SOL,'feed_id':'fixture'}]}}))
            original=SimpleNamespace(fetch=MagicMock(side_effect=ValueError('index unavailable')),
                observation=MagicMock(side_effect=ValueError('index unavailable')))
            sources=RecoverySources(original,config,root/'index.sqlite')
            client=MagicMock();adapter=MagicMock()
            adapter.snapshot.return_value={'base_balance':1000,'decimals':6,'read_at':100,'slot':10}
            adapter.output.return_value=10000000
            engine=SimpleNamespace(adapter_factory=MagicMock(return_value=adapter),rpc=MagicMock(),user='fixture')
            position={'mint':'fixture','pool':'pool','kind':'pumpswap','units':1000}
            with patch('position_recovery.time.time',return_value=100),patch('position_recovery.check_boundary'),patch('position_recovery.make_rpc',return_value=client),patch('position_recovery.capture_sol',return_value={}),patch('position_recovery.decode_sol',return_value={'usd':'100','publish_time':100,'confidence_bps':'1'}):
                self.assertEqual(sources.fetch('https://fixture.invalid/sol-price')['solPrice'],100)
                observation=sources.position_observation(engine,position)
                self.assertTrue(observation['exit_only'])
                self.assertFalse(observation['data_rights']['learning_allowed'])
                with self.assertRaises(ValueError):sources.observation('fixture')
            sources.close();client.close.assert_called_once()

    def test_recovery_rejects_a_valid_but_old_oracle_snapshot(self):
        sources=RecoverySources.__new__(RecoverySources)
        sources.oracle_cache=None;sources.client=MagicMock()
        sources.feed={};sources.endpoint={'policy':{'oracle_max_confidence_bps':100}}
        with patch('position_recovery.time.time',return_value=100),patch('position_recovery.check_boundary'),patch('position_recovery.capture_sol',return_value={}),patch('position_recovery.decode_sol',return_value={'usd':'100','publish_time':1,'confidence_bps':'1'}):
            with self.assertRaisesRegex(ValueError,'current oracle publication'):sources.oracle_price()
        self.assertIsNone(sources.oracle_cache)

    def test_recovery_quote_is_rejected_before_entry_or_marking(self):
        from meme_live import MemeLiveEngine
        engine=MemeLiveEngine.__new__(MemeLiveEngine)
        engine.store=SimpleNamespace(state={'pending':None,'position':None},select_position=MagicMock())
        engine.mark=MagicMock()
        result=engine.prepare({'mint':'fixture','exit_only':True},100,10,now=100)
        self.assertEqual(result['status'],'recovery_quote_cannot_open_position')
        engine.mark.assert_not_called()


class IndexedWalletTests(unittest.TestCase):
    def test_followed_wallet_leads_use_verified_index_and_do_not_become_labels(self):
        helper=fixtures.IndexerTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.populated()
        feed=IndexedWalletFeed(helper.path)
        with patch.object(feed.store,'watermark',return_value=3600),patch.object(feed.store,'coverage',return_value={'complete':True}):
            report=feed.poll([fixtures.USER],now=3600)
        self.assertEqual(report['status'],'bounded_sample')
        self.assertFalse(report['independent_human_verified'])
        self.assertFalse(report['automatic_trade_approval'])
        self.assertTrue(report['signals'])
        for group in report['signals']:
            for event in group['observations']:
                self.assertEqual(event['source'],'finalized_rpc_index')
                self.assertIsNone(event['buy_size_quote'])
        with patch.object(feed.store,'watermark',return_value=3000):
            self.assertEqual(feed.poll([fixtures.USER],now=3600)['status'],'finalized_wallet_index_stale')
