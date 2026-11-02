"""Bounded read-only batch contracts; no external RPC or operational state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from data_policy import SourceAccessBlocked
from rpc_contract import rpc_batch_result,RpcEvidenceError,RpcRemoteError
from onchain_ingest import IndexRpc,Ingester
from onchain_store import EventStore
from test_onchain_indexer import FakeRpc,POLICY


class BlockBatchTests(unittest.TestCase):
    def test_reply_ids_control_order_and_all_members_are_required(self):
        budget=MagicMock()
        rows=[{'jsonrpc':'2.0','id':2,'result':{'transactions':[],'blockhash':'second'}},
              {'jsonrpc':'2.0','id':1,'result':{'transactions':[],'blockhash':'first'}}]
        decoded=rpc_batch_result(json.dumps(rows),'fixture',budget,'https://fixture.invalid',2)
        self.assertEqual([row['blockhash'] for row in decoded],['first','second'])
        for invalid in (rows[:1],rows+rows,rows[:1]*2,[dict(rows[0],id=True),rows[1]],
                        [dict(rows[0],id=3),rows[1]],{'result':rows}):
            with self.assertRaises(RpcEvidenceError):
                rpc_batch_result(json.dumps(invalid),'fixture',budget,'https://fixture.invalid',2)
        with self.assertRaises(RpcEvidenceError):
            rpc_batch_result('[{"jsonrpc":"2.0","id":1,"id":2,"result":{}}]',
                             'fixture',budget,'https://fixture.invalid',1)

    def test_access_and_throttle_failures_keep_their_original_type(self):
        for code,error in ((403,SourceAccessBlocked),(429,RpcRemoteError)):
            budget=MagicMock()
            body=json.dumps([{'jsonrpc':'2.0','id':1,'error':{'code':code,'message':'fixture'}}])
            with self.assertRaises(error):rpc_batch_result(body,'fixture',budget,'https://fixture.invalid',1)
            self.assertEqual(budget.failure.call_args.args[1],code)

    def test_batch_size_does_not_change_the_shared_request_floor(self):
        for invalid in (0,3,True,1.5):
            with self.assertRaises(ValueError):
                IndexRpc({'url':'https://fixture.invalid','source_id':'fixture','block_batch_size':invalid})
        rpc=IndexRpc({'url':'https://fixture.invalid','source_id':'fixture','block_batch_size':2},MagicMock())
        for slots in ([],[1,1],[-1],[True],[2**63],[1,2,3]):
            with self.assertRaises(ValueError):rpc.call_blocks(slots,{})
        for payload in ({'method':'sendTransaction'},[{'method':'getSlot'},{'method':'getSlot'}]):
            with self.assertRaises(ValueError):rpc._post(payload)
        self.assertEqual(rpc.requests,0)
        rpc.budget.reserve.assert_not_called()

    def test_missing_usd_data_reports_the_underlying_oracle_failure(self):
        from runtime_health import health
        from wallet_watch import SOL
        with tempfile.TemporaryDirectory() as directory:
            store=EventStore(Path(directory)/'index.sqlite')
            report={'status':'incomplete','at':1000,'feeds':[{'mint':SOL,'error':'Oracle confidence exceeds reviewed policy'}]}
            with store.connect() as db:db.execute('INSERT INTO telemetry VALUES(?,?)',('oracle',json.dumps(report)))
            result=health(store.path,now=1000)
            self.assertFalse(result['ready'])
            self.assertIn('USD oracle capture failed: Oracle confidence exceeds reviewed policy',result['blockers'])

    def test_long_provider_cooldown_does_not_exhaust_actual_retry_budget(self):
        from provider_limits import ProviderBackoff
        from onchain_ingest import follow
        ingester=MagicMock()
        ingester.store.next_slot.return_value=5
        ingester.step.side_effect=[ProviderBackoff(600),ProviderBackoff(540),
                                  {'progress':{'lag_slots':0},'status':'indexed'}]
        with patch('onchain_ingest.time.sleep') as sleep:
            stream=follow(ingester,start=5,count=1,max_failures=1)
            self.assertEqual(next(stream)['retry_seconds'],600)
            self.assertEqual(next(stream)['retry_seconds'],540)
            self.assertEqual(next(stream)['status'],'indexed')
            stream.close()
        self.assertEqual(sleep.call_args_list[0].args,(60,))
        self.assertEqual(ingester.step.call_args_list[-1].args[0],None)

    def test_two_block_commit_preserves_contiguous_prefix_on_second_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store=EventStore(Path(directory)/'index.sqlite');store.register_source('fixture',POLICY)
            rpc=FakeRpc();rpc.block_batch_size=2
            rpc.call_blocks=lambda slots,options:[rpc.call('getBlock',[slots[0],options]),None]
            ingester=Ingester(store,[rpc])
            with patch.object(ingester,'deployments',return_value={p:0 for p in ingester.decoder.specs}):
                with self.assertRaises(ValueError):ingester.step(1,2)
                self.assertEqual(store.next_slot(),2)
                self.assertEqual(store.progress()['next_slot'],2)
                self.assertEqual(store.progress()['lag_slots'],1)
                rpc.call_blocks=lambda slots,options:[rpc.call('getBlock',[slot,options]) for slot in slots]
                ingester.step(count=1)
            self.assertEqual(store.next_slot(),3)
            self.assertEqual(store.metrics()['blocks'],2)
