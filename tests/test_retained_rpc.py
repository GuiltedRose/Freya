import tempfile
import unittest
from pathlib import Path

from onchain_store import EventStore
from retained_rpc import RpcError, dispatch


class RetainedRpcTests(unittest.TestCase):
    def test_only_serves_retained_unquarantined_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.sqlite'
            writer = EventStore(path, retain_blocks=True)
            writer.register_source('fixture', {'storage_verified': True, 'evidence': 'offline test'})
            for number, issues in ((1, []), (2, ['missing receipt'])):
                block = {'blockhash': str(number), 'previousBlockhash': str(number - 1),
                         'parentSlot': number - 1, 'blockTime': 60 * number,
                         'transactions': [], '_indexer_deployments': {}}
                writer.commit_range(number, number, [number], [(number, block, [], issues, [])], 'fixture')
            store = EventStore(path, readonly=True)
            result = dispatch(store, 'getBlock', [1, {'commitment': 'finalized', 'rewards': False}])
            self.assertEqual(result['blockhash'], '1')
            self.assertNotIn('_indexer_deployments', result)
            self.assertFalse(dispatch(store, 'freya_getStatus', [])['live_rpc_ready'])
            for method, params, code in (
                ('getBlock', [2], -32004), ('getBlock', [3], -32004),
                ('getBlock', [1, {'commitment': 'confirmed'}], -32602),
                ('getAccountInfo', [], -32601),
            ):
                with self.subTest(method=method, params=params), self.assertRaises(RpcError) as error:
                    dispatch(store, method, params)
                self.assertEqual(error.exception.code, code)
            with writer.connect() as db:
                db.execute("UPDATE raw_blocks SET raw_hash='invalid' WHERE slot=1")
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                dispatch(store, 'getBlock', [1])
