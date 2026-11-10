"""Integrated synthetic RPC-to-paper-and-learning contract. Never uses a wallet."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
from types import SimpleNamespace
import test_onchain_indexer as chain
from test_momentum_reclaim import policy
from onchain_store import EventStore
from onchain_ingest import Ingester
from onchain_decode import Decoder
from onchain_market import OnchainPaperSources
from onchain_candles import CandleEngine
from paper_runtime import PaperSession
from production_strategy import validate


class ProductionPipelineTests(unittest.TestCase):
    def test_only_reclaim_is_a_production_strategy(self):
        self.assertIsNotNone(validate(policy()))
        with self.assertRaises(ValueError):
            validate(replace(policy(),momentum_reclaim_enabled=False))
        with self.assertRaises(ValueError):
            validate(replace(policy(),entry_style='trend_pullback'))

    def test_rpc_events_candles_entry_restart_exit_and_learning(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            store=EventStore(root/'index.sqlite')
            store.register_source('test',dict(chain.POLICY,ml_verified=True,evaluation_verified=True))
            blocks={1:chain.block(1,0)}
            slot=1
            for minute,prices,amount in (
                (3420,(1,1.25,.98,1.2),1_000_000_000),
                (3480,(1.2,1.21,1,1.08),2_000_000_000),
                (3540,(1.08,1.24,1.02,1.22),3_000_000_000)):
                store.add_conversion(chain.SOL,minute,100,'test',{'synthetic_fixture':True})
                for offset,price in zip((0,10,20,50),prices):
                    slot+=1
                    tx=chain.transaction(amount=amount,quote=round(price*amount*10),signature='fixture-'+str(slot))
                    blocks[slot]=chain.block(slot,minute+offset,[tx])
            slot+=1
            blocks[slot]=chain.block(slot,3600)
            store.add_conversion(chain.SOL,3600,100,'test',{'synthetic_fixture':True})
            requests=[]
            def call(method,params):
                requests.append(method)
                if method=='getSlot':return max(blocks)
                if method=='getFirstAvailableBlock':return min(blocks)
                if method=='getBlocks':return list(range(params[0],params[1]+1))
                if method=='getBlock':return blocks[params[0]]
                raise AssertionError('Unexpected RPC method: '+method)
            client=SimpleNamespace(source='test',validate=lambda:None,call=call)
            decoder=Decoder()
            versions={p:[dict(from_slot=0,through_slot=100,deployment_slot=0,idl_hash=s['hash'])]
                      for p,s in decoder.specs.items()}
            ingester=Ingester(store,[client],versions)
            with patch.object(ingester,'deployments',return_value={p:0 for p in decoder.specs}):
                ingester.step(1,64)
            candles=CandleEngine(store)
            candles.watch(chain.MINT,chain.POOL)
            candles.sync()
            source=OnchainPaperSources(store.path)
            with patch('onchain_market.time.time',return_value=3600):
                snapshot=source.observation(chain.MINT,3600,chain.POOL)
            session=PaperSession(root/'paper',0,policy(),.065,100)
            try:
                self.assertEqual(session.process(snapshot)['action'],'paper_buy')
                self.assertIsNone(session.process(snapshot))
            finally:session.close()
            session=PaperSession(root/'paper')
            try:
                self.assertIsNotNone(session.trader.position)
                slot+=1
                tx=chain.transaction(amount=1_000_000_000,quote=30_000_000_000,signature='fixture-exit')
                blocks[slot]=chain.block(slot,3610,[tx])
                with patch.object(ingester,'deployments',return_value={p:0 for p in decoder.specs}):
                    ingester.step(count=64)
                with patch('onchain_market.time.time',return_value=3610):
                    quote=source.observation(chain.MINT,3610,chain.POOL)
                self.assertEqual(session.process(quote)['action'],'paper_sell')
                self.assertEqual(session.refresh_learning()['samples'],1)
                self.assertIsNone(session.trader.position)
                self.assertIn('getBlock',requests)
                self.assertNotIn('sendTransaction',requests)
            finally:session.close()

    def test_active_model_veto_prevents_entry(self):
        from test_momentum_reclaim import setup
        with tempfile.TemporaryDirectory() as root:
            session=PaperSession(root,0,policy(),.065,100)
            session.decision_models=SimpleNamespace(entry=lambda *a,**k:{'allow':False,'status':'blocked'})
            try:
                event=session.process(setup())
                self.assertEqual(event['reason'],'unified model entry veto')
                self.assertIsNone(session.trader.position)
            finally:session.close()

    def test_model_failure_cannot_block_an_existing_stop_exit(self):
        from test_momentum_reclaim import setup
        with tempfile.TemporaryDirectory() as root:
            session=PaperSession(root,0,policy(),.065,100)
            try:
                snapshot=setup()
                self.assertEqual(session.process(snapshot)['action'],'paper_buy')
                def unavailable(*args,**kwargs):raise AssertionError('Exit must bypass entry-model veto')
                session.decision_models=SimpleNamespace(entry=unavailable)
                snapshot=dict(snapshot,observed_at=1201,price_usd=.1)
                snapshot['execution']=dict(snapshot['execution'],observed_at=1201)
                self.assertEqual(session.process(snapshot)['action'],'paper_sell')
            finally:session.close()
