"""Prepared offline checks; not executed by the implementation agent."""
import tempfile
import unittest
from pathlib import Path
from run_selective_node import owned_directory,source_config


class SelectiveNodeTests(unittest.TestCase):
    def test_native_mode_never_falls_back(self):
        with self.assertRaises(ValueError):source_config({},'native')

    def test_existing_state_without_owner_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root);existing=path/'paper.sqlite';existing.write_bytes(b'preserve')
            with self.assertRaises(ValueError):owned_directory(path)
            self.assertEqual(existing.read_bytes(),b'preserve')

    def test_owned_directory_resumes_without_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'node';owned_directory(path)
            journal=path/'index.sqlite';journal.write_bytes(b'preserve')
            owned_directory(path)
            self.assertEqual(journal.read_bytes(),b'preserve')
