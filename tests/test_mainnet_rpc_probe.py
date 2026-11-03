"""The operator-only probe is tested with fake RPC calls, never mainnet."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import io
import json
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from diagnose_mainnet_rpc import worker
from wallet_watch import MAINNET


class MainnetProbeTests(unittest.TestCase):
    def test_same_finalized_blocks_and_ten_counted_requests(self):
        config={'endpoints':[{'enabled':True,'url':'https://fixture.invalid','source_id':'fixture'}]}
        decoder=MagicMock();decoder.transaction.return_value=([],[])
        calls=[]
        class Rpc:
            url='https://fixture.invalid'
            interval=.5;block_batch_size=1;burst_requests=10;burst_cooldown_seconds=30
            budget=SimpleNamespace(path=Path('/tmp/probe-fake-ledger'))
            wire_bytes=0
            def call(self,method,params):
                calls.append((method,params));self.wire_bytes+=100
                return {'getGenesisHash':MAINNET,'getSlot':100,'getBlocks':[98,99,100],
                        'getBlock':{'transactions':[{}],'blockTime':1000}}[method]
            def close(self):pass
        with tempfile.TemporaryDirectory() as root:
            args=SimpleNamespace(config=Path(root)/'config',index_db=Path(root)/'index.sqlite',max_requests=10)
            with patch('pipeline_runtime.read_json',return_value=config),patch('pipeline_runtime.resolve_indexer',return_value=config),\
                 patch('runtime_health.validate_config',return_value=decoder),patch('solana_rpc_compat.check_boundary'),\
                 patch('pipeline_runtime.writer_lock',return_value=nullcontext()),\
                 patch('provider_limits.ProviderBudget') as budget,patch('solana_rpc_compat.make_rpc',side_effect=lambda *a,**k:Rpc()),\
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                budget.return_value.cooldown_remaining.return_value=0
                budget.return_value.identity.return_value={}
                worker(args)
            rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(calls),10)
        self.assertEqual(calls[:5],calls[5:])
        self.assertEqual(sum(row['method']=='getBlock' for row in rows),4)
        self.assertEqual(decoder.transaction.call_count,4)
        self.assertTrue(all(row['status']=='success' for row in rows))

    def test_timeout_envelope_restores_process_timer_and_classifies_connect(self):
        import signal
        import http.client
        from diagnose_mainnet_rpc import matched_python_timeouts,ConnectTimeout
        from rpc_diagnostics import transport_error
        import time
        previous=signal.getsignal(signal.SIGALRM)
        with matched_python_timeouts():
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0],0)
            error=transport_error(ConnectTimeout(),time.monotonic())
            self.assertEqual(error.category,'connect_timeout')
            self.assertEqual(error.phase,'connect')
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        self.assertIs(signal.getsignal(signal.SIGALRM),previous)

    def test_subprocess_strips_signers_and_deadline_keeps_partial_evidence(self):
        import subprocess
        import os
        from diagnose_mainnet_rpc import main
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/'report.json'
            argv=['diagnose_mainnet_rpc.py','--config',str(Path(root)/'config.json'),
                  '--index-db',str(Path(root)/'index.sqlite'),'--output',str(output),
                  '--allow-mainnet','--workers-stopped']
            partial=b'{"status":"success","backend":"python","method":"getSlot"}\n'
            with patch.dict(os.environ,{'SOLANA_PRIVATE_KEY':'SECRET','SOLANA_KEYPAIR_PATH':'/SECRET'}),\
                 patch('sys.argv',argv),patch('sys.stdout',new_callable=io.StringIO),\
                 patch('diagnose_mainnet_rpc.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',180,output=partial)) as run:
                with self.assertRaises(SystemExit) as caught:main()
            self.assertEqual(caught.exception.code,1)
            self.assertNotIn('SOLANA_PRIVATE_KEY',run.call_args.kwargs['env'])
            self.assertNotIn('SOLANA_KEYPAIR_PATH',run.call_args.kwargs['env'])
            report=json.loads(output.read_text())
            self.assertTrue(report['deadline_reached'])
            self.assertTrue(report['inflight_request_unknown'])
            self.assertFalse(report['diagnostic_complete'])
            self.assertEqual(len(report['results']),1)
            self.assertEqual(output.stat().st_mode & 0o777,0o600)
            self.assertNotIn('SECRET',output.read_text())
