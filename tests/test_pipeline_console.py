"""Unexecuted terminal regression contracts; no child processes or RPC calls."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pipeline_console import WorkerOutput,format_pipeline


class PipelineConsoleTests(unittest.TestCase):
    def test_existing_trade_and_learning_console_reaches_terminal_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'consumer.log';path.write_text('old session\n')
            follower=WorkerOutput();follower.follow('consumer',path)
            with path.open('a') as stream:stream.write('BUY fixture | $1.00\nPAPER | equity $10.00\nNN | 0 samples\n')
            output=io.StringIO()
            with patch('sys.stdout',output):follower.poll();follower.poll()
            self.assertEqual(output.getvalue(),'BUY fixture | $1.00\nPAPER | equity $10.00\nNN | 0 samples\n')
            self.assertIn('old session',path.read_text())

    def test_learner_metrics_and_crash_are_visible_without_dumping_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'learning.log'
            follower=WorkerOutput();follower.follow('learning',path)
            path.write_text(json.dumps({'pipeline':{'models':{'rag':{'status':'blocked','reason':'No eligible data'}}}})+'\n'
                +'sqlite3.OperationalError: no such column: source https://provider.invalid/SECRET\n')
            output=io.StringIO()
            with patch('sys.stdout',output):follower.poll()
            self.assertIn('RAG: blocked',output.getvalue())
            self.assertIn('no such column: source',output.getvalue())
            self.assertNotIn('SECRET',output.getvalue())

    def test_partial_lines_and_json_output_preserve_complete_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'consumer.log';follower=WorkerOutput(json_output=True)
            follower.follow('consumer',path);path.write_text('PAPER | equ')
            output=io.StringIO()
            with patch('sys.stdout',output):
                follower.poll()
                self.assertEqual(output.getvalue(),'')
                with path.open('a') as stream:stream.write('ity $10\n')
                follower.poll()
            self.assertEqual(json.loads(output.getvalue()),{'worker':'consumer','output':'PAPER | equity $10'})

    def test_pipeline_summary_keeps_lag_and_failure_visible(self):
        text=format_pipeline({'status':'degraded','failed_role':'learning','exit_code':1,
            'progress':{'indexed_slots_per_second':.12,'lag_slots':470}})
        self.assertIn('learning exited 1',text)
        self.assertIn('lag 470 slots',text)
