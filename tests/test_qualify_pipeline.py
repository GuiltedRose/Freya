import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qualify_pipeline import deployment_candidate


class DeploymentCandidateTests(unittest.TestCase):
    def test_finalized_receipt_and_stable_deployment_required(self):
        decoder = SimpleNamespace(
            specs={'program': {'venue': 'pump', 'hash': 'schema'}},
            transaction=Mock(return_value=([{'kind': 'swap', 'venue': 'pump'}], [])),
        )
        rpc = Mock(source='reviewed-source')
        receipt = {'slot': 9, 'meta': {'err': None}, 'transaction': {'signatures': ['receipt']}}
        rpc.call.side_effect = [receipt, 10]
        with patch('qualify_pipeline.read_program_deployments', side_effect=[{'program': 8}, {'program': 8}]):
            candidate = deployment_candidate(rpc, decoder, {'pump': ['receipt']})
        self.assertEqual(candidate['status'], 'candidate_requires_review')
        self.assertEqual(candidate['candidate_program_versions']['program'][0]['from_slot'], 10)
        rpc.call.side_effect = [receipt, 10]
        with patch('qualify_pipeline.read_program_deployments', side_effect=[{'program': 8}, {'program': 9}]):
            with self.assertRaisesRegex(ValueError, 'changed during sampling'):
                deployment_candidate(rpc, decoder, {'pump': ['receipt']})
