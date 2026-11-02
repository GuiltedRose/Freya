"""Unexecuted setup contracts; all RPC probes are mocked."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from contextlib import ExitStack
from pipeline_setup import automatic_profile,configure,save_documents
from run_bot_pipeline import template_documents


class SetupTests(unittest.TestCase):
    def test_selfhosted_budget_boundary_failure_cannot_be_bypassed_by_automatic_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=SimpleNamespace(mode='paper',indexer_config=root/'indexer.json',learning_config=root/'learning.json',
                onchain_db=root/'index.sqlite',paper_state=root/'paper',live_state=root/'live')
            with patch('builtins.input',side_effect=AssertionError('No questions')),patch('sys.stdout',io.StringIO()),patch('solana_rpc_compat.check_boundary',side_effect=ValueError('Budget unavailable')),patch('solana_rpc_compat.make_rpc') as rpc:
                with self.assertRaisesRegex(ValueError,'Budget unavailable'):
                    configure(args,{'SOLANA_RPC_URL':'http://127.0.0.1:8899'},*template_documents(args))
            rpc.assert_not_called()
            self.assertFalse(args.indexer_config.exists())
            self.assertFalse(args.learning_config.exists())

    def test_profile_binding_does_not_trust_provider_lookalikes_or_userinfo(self):
        self.assertEqual(automatic_profile({'SOLANA_RPC_URL':'https://api.mainnet-beta.solana.com'}),'public_mainnet')
        self.assertIsNone(automatic_profile({'SOLANA_RPC_URL':'https://api.mainnet.solana.com.example.invalid'}))
        self.assertIsNone(automatic_profile({'SOLANA_RPC_URL':'https://api.mainnet.solana.com@evil.invalid'}))
        self.assertIsNone(automatic_profile({'SOLANA_RPC_URL':'https://api.mainnet.solana.com/custom'}))
        self.assertEqual(automatic_profile({'SOLANA_RPC_URL':'http://127.0.0.1:8899'}),'selfhosted')
        self.assertEqual(automatic_profile({'SOLANA_RPC_URL':'https://192.168.1.2:8899'}),'selfhosted')
        self.assertEqual(automatic_profile({'SOLANA_RPC_URL':'https://my-node.example.invalid','CRYPTO_BOT_RPC_MODE':'selfhosted'}),'selfhosted')
        with self.assertRaises(ValueError):automatic_profile({'SOLANA_RPC_URL':'http://192.168.1.2:8899'})

    def test_replacement_retains_exact_backup_and_rejects_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';old=b'{"original": true}\n';path.write_bytes(old)
            backups=save_documents([(path,{'new':True})],{path:old})
            self.assertEqual(Path(backups[0]).read_bytes(),old)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError):save_documents([(path,{'other':True})],{path:old})
            self.assertEqual(json.loads(path.read_text()),{'new':True})

    def test_builtin_profiles_need_no_prompts_for_paper_or_live_and_preserve_network_boundary(self):
        for mode,url in ((mode,url) for mode in ('paper','live') for url in ('https://api.mainnet-beta.solana.com','http://127.0.0.1:8899')):
            with self.subTest(mode=mode,url=url),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                root=Path(directory)
                args=SimpleNamespace(mode=mode,indexer_config=root/'indexer.json',learning_config=root/'learning.json',
                    onchain_db=root/'index.sqlite',paper_state=root/'paper',live_state=root/'live')
                indexer,learning=template_documents(args)
                rpc=MagicMock();rpc.call.return_value=10
                decoder=SimpleNamespace(specs={'program':{'hash':'pinned'}})
                stack.enter_context(patch('sys.stdin.isatty',return_value=False))
                stack.enter_context(patch('builtins.input',side_effect=AssertionError('Built-in profiles must never prompt')))
                stack.enter_context(patch('sys.stdout',io.StringIO()))
                boundary=stack.enter_context(patch('solana_rpc_compat.check_boundary'))
                stack.enter_context(patch('solana_rpc_compat.make_rpc',return_value=rpc))
                stack.enter_context(patch('solana_rpc_compat.close_rpc_clients'))
                stack.enter_context(patch('onchain_decode.Decoder',return_value=decoder))
                stack.enter_context(patch('onchain_ingest.read_program_deployments',return_value={'program':5}))
                stack.enter_context(patch('onchain_oracle.capture_sol',return_value={}))
                stack.enter_context(patch('onchain_oracle.decode_sol',return_value={'confidence_bps':'1.25'}))
                stack.enter_context(patch('pipeline_setup.preflight',return_value=[]))
                configure(args,{'SOLANA_RPC_URL':url},indexer,learning)
                saved=json.loads(args.indexer_config.read_text())
                self.assertEqual(saved['reviewed_program_versions']['program'][0]['from_slot'],10)
                self.assertEqual(saved['reviewed_program_versions']['program'][0]['deployment_slot'],5)
                if mode=='live':self.assertTrue(saved['endpoints'][0]['policy']['execution_verified'])
                self.assertFalse(args.onchain_db.exists())
                self.assertFalse(args.paper_state.exists())
                self.assertFalse(args.live_state.exists())
                boundary.assert_called_once()
                if url.startswith('http:'):
                    self.assertEqual(saved['endpoints'][0]['network_mode'],'isolated')
                    self.assertTrue(saved['endpoints'][0]['network_budget'])
                else:self.assertNotIn('network_budget',saved['endpoints'][0])
                rpc.call.assert_called_once_with('getSlot',[{'commitment':'finalized'}])

    def test_declining_setup_creates_no_configuration_or_rpc_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=SimpleNamespace(mode='paper',indexer_config=root/'indexer.json',learning_config=root/'learning.json',
                onchain_db=root/'index.sqlite',paper_state=root/'paper',live_state=root/'live')
            with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='no'),patch('sys.stdout',io.StringIO()),patch('solana_rpc_compat.make_rpc') as rpc:
                with self.assertRaises(ValueError):configure(args,{'SOLANA_RPC_URL':'https://custom.example.invalid'},*template_documents(args))
            rpc.assert_not_called()
            self.assertEqual(list(root.iterdir()),[])
