"""Active model decision contracts; mocked scores are not qualification evidence."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from learning_pipeline import DecisionModels
from unified_observations import ObservationStore
from onchain_market import SOURCE
from test_momentum_reclaim import policy, setup


class ModelDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        ObservationStore(root/'models.sqlite',create=True)
        config={'rights':{'storage_verified':True,'ml_verified':True,'evaluation_verified':True,'evidence':'unit fixture'},
                'provenance':{'verified':True,'evidence':'unit fixture'},'ledgers':[],
                'candidates':{'svm':'svm','lstm':'lstm','rag':'rag'}}
        (root/'config.json').write_text(json.dumps(config))
        self.models=DecisionModels(root/'models.sqlite',root/'config.json')
        self.snapshot=setup()
        self.snapshot.update(market_source_url=SOURCE,trades_source_url=SOURCE)
        self.snapshot['candle_history']['source_url']=SOURCE

    def candidate(self,store,identifier):
        return {'family':identifier,'created_at':time.time()-1}

    def test_low_score_actively_vetoes_and_all_scores_can_allow_paper(self):
        with patch('learning_pipeline.load_candidate',side_effect=self.candidate), patch('learning_pipeline.forecast',return_value={'score':.2}):
            self.assertFalse(self.models.entry(self.snapshot,policy())['allow'])
        self.snapshot['observed_at']+=1
        with patch('learning_pipeline.load_candidate',side_effect=self.candidate), patch('learning_pipeline.forecast',return_value={'score':.9}):
            self.assertTrue(self.models.entry(self.snapshot,policy())['allow'])

    def test_high_scores_do_not_bypass_live_qualification(self):
        with patch('learning_pipeline.load_candidate',side_effect=self.candidate), patch('learning_pipeline.forecast',return_value={'score':.9}), patch('unified_learning.evaluate_candidate',return_value={'status':'collecting','live_eligible':False}) as qualification:
            self.assertFalse(self.models.entry(self.snapshot,policy(),live=True)['allow'])
            self.assertEqual(qualification.call_count,3)

    def test_missing_family_blocks_entry(self):
        self.models.config['candidates']={}
        self.assertFalse(self.models.entry(self.snapshot,policy())['allow'])
