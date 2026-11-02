"""Offline regression coverage for reused HTTP setup and native identity handling."""
import json
import unittest
from unittest.mock import MagicMock,patch
from data_policy import SourceAccessBlocked
from onchain_ingest import IndexRpc
from solana_rpc_compat import RustIndexRpc


class RpcResponseEfficiencyTests(unittest.TestCase):
    def test_python_reuses_opener_but_paces_each_request(self):
        budget=MagicMock()
        rpc=IndexRpc({'url':'https://example.invalid','source_id':'fixture'},budget)
        with patch('onchain_ingest.build_opener') as factory:
            response=factory.return_value.open.return_value.__enter__.return_value
            response.headers={}
            response.read.return_value=b'{"jsonrpc":"2.0","id":1,"result":7}'
            for _ in range(3):self.assertEqual(rpc.call('getSlot',[]),7)
            factory.assert_called_once()
            self.assertEqual(factory.return_value.open.call_count,3)
            self.assertEqual(budget.reserve.call_count,3)
            self.assertEqual(rpc.requests,3)

    def test_identity_retains_utf8_byte_accounting_and_json_validation(self):
        rpc=RustIndexRpc({'url':'https://example.invalid','source_id':'fixture'},MagicMock())
        text=json.dumps({'jsonrpc':'2.0','id':1,'result':'é'},ensure_ascii=False)
        self.assertEqual(rpc._finish_encoded(200,'','identity',text,method='getGenesisHash'),'é')
        self.assertEqual(rpc.wire_bytes,len(text.encode('utf8')))
        self.assertEqual(rpc.decoded_bytes,rpc.wire_bytes)
        for malformed in ('not-json','{"jsonrpc":"2.0","id":2,"result":7}','\ud800'):
            with self.subTest(body=repr(malformed)),self.assertRaises(SourceAccessBlocked):
                rpc._finish_encoded(200,'','identity',malformed,method='getSlot')

    def test_identity_batch_keeps_response_id_contract(self):
        rpc=RustIndexRpc({'url':'https://example.invalid','source_id':'fixture'},MagicMock())
        rows=[{'jsonrpc':'2.0','id':2,'result':{'transactions':[],'blockHeight':8}},{'jsonrpc':'2.0','id':1,'result':{'transactions':[],'blockHeight':7}}]
        self.assertEqual(rpc._finish_encoded(200,'','identity',json.dumps(rows),method='getBlock',batch_count=2),[rows[1]['result'],rows[0]['result']])
        rows[0]['id']=1
        with self.assertRaises(SourceAccessBlocked):
            rpc._finish_encoded(200,'','identity',json.dumps(rows),method='getBlock',batch_count=2)
