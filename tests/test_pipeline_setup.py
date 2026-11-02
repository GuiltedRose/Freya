"""Unexecuted setup contracts; all RPC probes are mocked."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from contextlib import ExitStack
from pipeline_setup import configure,save_documents
from run_bot_pipeline import template_documents


class SetupTests(unittest.TestCase):
    def test_replacement_retains_exact_backup_and_rejects_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';old=b'{"original": true}\n';path.write_bytes(old)
            backups=save_documents([(path,{'new':True})],{path:old})
            self.assertEqual(Path(backups[0]).read_bytes(),old)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError):save_documents([(path,{'other':True})],{path:old})
            self.assertEqual(json.loads(path.read_text()),{'new':True})

    def test_paper_and_live_setup_use_same_readonly_probes_and_never_create_trading_state(self):
        for mode in ('paper','live'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                root=Path(directory)
                args=SimpleNamespace(mode=mode,indexer_config=root/'indexer.json',learning_config=root/'learning.json',
                    onchain_db=root/'index.sqlite',paper_state=root/'paper',live_state=root/'live')
                indexer,learning=template_documents(args)
                rpc=MagicMock();rpc.call.return_value=10
                decoder=SimpleNamespace(specs={'program':{'hash':'pinned'}})
                def reply(prompt):
                    if 'Type yes' in prompt:return 'yes'
                    if 'basis points' in prompt:return '100'
                    if 'feed ID' in prompt or 'Oracle shard' in prompt:return ''
                    return 'operator reviewed fixture evidence'
                stack.enter_context(patch('sys.stdin.isatty',return_value=True))
                stack.enter_context(patch('builtins.input',side_effect=reply))
                stack.enter_context(patch('sys.stdout',io.StringIO()))
                stack.enter_context(patch('solana_rpc_compat.check_boundary'))
                stack.enter_context(patch('solana_rpc_compat.make_rpc',return_value=rpc))
                stack.enter_context(patch('solana_rpc_compat.close_rpc_clients'))
                stack.enter_context(patch('onchain_decode.Decoder',return_value=decoder))
                stack.enter_context(patch('onchain_ingest.read_program_deployments',return_value={'program':5}))
                stack.enter_context(patch('onchain_oracle.capture_sol',return_value={}))
                stack.enter_context(patch('onchain_oracle.decode_sol',return_value={'confidence_bps':'1.25'}))
                stack.enter_context(patch('pipeline_setup.preflight',return_value=[]))
                configure(args,{'SOLANA_RPC_URL':'https://api.mainnet-beta.solana.com'},indexer,learning)
                saved=json.loads(args.indexer_config.read_text())
                self.assertEqual(saved['reviewed_program_versions']['program'][0]['from_slot'],10)
                self.assertEqual(saved['reviewed_program_versions']['program'][0]['deployment_slot'],5)
                if mode=='live':self.assertTrue(saved['endpoints'][0]['policy']['execution_verified'])
                self.assertFalse(args.onchain_db.exists())
                self.assertFalse(args.paper_state.exists())
                self.assertFalse(args.live_state.exists())
                rpc.call.assert_called_once_with('getSlot',[{'commitment':'finalized'}])

    def test_declining_setup_creates_no_configuration_or_rpc_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=SimpleNamespace(mode='paper',indexer_config=root/'indexer.json',learning_config=root/'learning.json',
                onchain_db=root/'index.sqlite',paper_state=root/'paper',live_state=root/'live')
            with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='no'),patch('sys.stdout',io.StringIO()),patch('solana_rpc_compat.make_rpc') as rpc:
                with self.assertRaises(ValueError):configure(args,{'SOLANA_RPC_URL':'https://api.mainnet-beta.solana.com'},*template_documents(args))
            rpc.assert_not_called()
            self.assertEqual(list(root.iterdir()),[])
