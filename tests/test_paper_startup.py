"""Paper startup/initialization contracts; temporary state only."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from run_paper import main
from paper_runtime import PaperSession


class StartupTests(unittest.TestCase):
    def reject_before_session(self,arguments):
        stderr=io.StringIO()
        with patch('sys.argv',['run_paper.py',*arguments]),patch('sys.stderr',stderr),patch('run_paper.PaperSession') as session:
            with self.assertRaises(SystemExit) as error:main()
            self.assertEqual(error.exception.code,2)
            session.assert_not_called()
        return stderr.getvalue()

    def test_discovery_explains_indexer_dependency_without_opening_state(self):
        message=self.reject_before_session(['--discover','--loop'])
        self.assertIn('--onchain-db state/onchain/index.sqlite',message)
        self.assertIn('does not start an indexer',message)
        self.assertIn('--feed',message)

    def test_missing_index_is_rejected_before_creating_a_paper_run(self):
        with tempfile.TemporaryDirectory() as directory:
            missing=Path(directory)/'not-created.sqlite'
            message=self.reject_before_session(['--discover','--onchain-db',str(missing)])
            self.assertIn('existing finalized RPC index',message)
            self.assertFalse(missing.exists())

    def call(self,arguments):
        with patch('sys.argv',['run_paper.py',*arguments]),patch('sys.stdout',io.StringIO()):main()

    def test_explicit_initialize_and_resume_preserve_inventory_and_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'paper'
            self.call(['--state-dir',str(path),'--initialize','--balance','7','--native-sol','.2','--sol-price','123'])
            session=PaperSession(path)
            try:
                before=(session.run_id,session.trader.cash,session.trader.native_sol,session.trader.sol_price_usd)
            finally:session.close()
            self.call(['--state-dir',str(path),'--status'])
            session=PaperSession(path)
            try:self.assertEqual((session.run_id,session.trader.cash,session.trader.native_sol,session.trader.sol_price_usd),before)
            finally:session.close()
            self.assertIn('already exists',self.reject_before_session(['--state-dir',str(path),'--initialize']))

    def test_initialize_defaults_are_virtual_and_status_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'paper'
            self.assertIn('No paper session',self.reject_before_session(['--state-dir',str(path),'--status']))
            self.assertFalse(path.exists())
            self.call(['--state-dir',str(path),'--initialize'])
            session=PaperSession(path)
            try:
                self.assertEqual(session.initial,{'balance':0.,'native_sol':.1,'sol_price_usd':100.})
            finally:session.close()
