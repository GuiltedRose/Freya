"""Bounded acceptance observer tests; synthetic snapshots, no network or bot state."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rpc_run_evidence import main


class EvidenceMonitorTests(unittest.TestCase):
    def run_monitor(self,fresh):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'evidence.jsonl';clock=[1000.]
            samples=[{'ready':True,'usable_market_count':1,'progress':{'next_slot':n,'lag_is_current':fresh}} for n in (10,11,12)]
            argv=['rpc_run_evidence.py','--index-db',str(Path(root)/'index.sqlite'),
                  '--provider-ledger',str(Path(root)/'providers.sqlite'),'--output',str(out),
                  '--duration-seconds','20','--interval-seconds','10']
            with patch('sys.argv',argv),patch('sys.stdout',new_callable=io.StringIO),\
                 patch('rpc_run_evidence.observation',side_effect=samples),\
                 patch('rpc_run_evidence.ledger_report',return_value={'available':False}),\
                 patch('rpc_run_evidence.legacy_logs',return_value={}),\
                 patch('rpc_run_evidence.namespace_settings',return_value={}),\
                 patch('rpc_run_evidence.time.time',side_effect=lambda:clock[0]),\
                 patch('rpc_run_evidence.time.monotonic',side_effect=lambda:clock[0]),\
                 patch('rpc_run_evidence.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),\
                 patch('socket.socket',side_effect=AssertionError('Observer must never open a network socket')):
                try:main();code=0
                except SystemExit as error:code=error.code
            return code,[json.loads(line) for line in out.read_text().splitlines()]

    def test_three_fresh_complete_samples_and_checkpoint_advance(self):
        code,rows=self.run_monitor(True)
        self.assertEqual(code,0)
        self.assertTrue(rows[-1]['sustained_data_milestone_observed'])
        self.assertFalse(rows[-1]['paper_ml_accepted'])
        self.assertEqual(rows[-1]['network_requests'],0)
        self.assertEqual(len([row for row in rows if row['kind']=='sample']),3)

    def test_stale_head_cannot_pass_just_because_saved_lag_is_small(self):
        code,rows=self.run_monitor(False)
        self.assertEqual(code,1)
        self.assertFalse(rows[-1]['sustained_data_milestone_observed'])

    def test_external_report_settings_never_copy_raw_urls_or_parameters(self):
        from rpc_run_evidence import safe_settings
        settings=safe_settings({'provider_ledger':'https://SECRET','provider_id':'SECRET',
                                'endpoint_id':{'url':'SECRET'},'url':'https://SECRET',
                                'parameters':{'api_key':'SECRET'},'backend':'rust'})
        self.assertNotIn('SECRET',json.dumps(settings))
        self.assertEqual(settings['backend'],'rust')
