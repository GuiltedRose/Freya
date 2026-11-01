"""UNEXECUTED regressions. Temporary databases and mocked RPC only."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_onchain_indexer as fixtures
from onchain_candles import CandleEngine
from onchain_market import OnchainPaperSources
from onchain_decode import Decoder
from runtime_health import validate_config, health, safe_reason


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.helper=fixtures.IndexerTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.market=self.helper.populated()

    def config(self):
        return {'endpoints':[{'enabled':True,'source_id':'fixture','url':'https://example.invalid',
                'policy':fixtures.POLICY}], 'reviewed_program_versions':{
                program:[{'from_slot':0,'through_slot':100,'deployment_slot':0,'idl_hash':spec['hash']}]
                for program,spec in Decoder().specs.items()}}

    def test_unwatched_market_uses_verified_events_despite_cache_table(self):
        CandleEngine(self.helper.store)
        row=self.market.observation(fixtures.MINT,3600,fixtures.POOL)
        self.assertEqual(row['buy_volume_usd'],600)
        self.assertEqual(len(row['candle_history']['candles']),3)
        self.assertFalse(CandleEngine(self.helper.store).available(fixtures.MINT,fixtures.POOL))

    def test_collection_timestamp_never_refreshes_stale_price(self):
        source=OnchainPaperSources(self.helper.path)
        row=self.market.observation(fixtures.MINT,3600,fixtures.POOL)
        row['observed_at']=3590
        with patch.object(source.local,'observation',return_value=row):
            current=source.market_observation(fixtures.MINT,3600)
        self.assertEqual((current['observed_at'],current['price_as_of']),(3600,3590))
        stale=dict(row,observed_at=3500,price_as_of=3500)
        with patch.object(source.local,'observation',return_value=stale):
            with self.assertRaisesRegex(ValueError,'older than 30'):
                source.market_observation(fixtures.MINT,3600)

    def test_preflight_rejects_missing_versions_and_permissions_without_network(self):
        config=self.config()
        self.assertEqual(len(validate_config(config).specs),2)
        config['reviewed_program_versions']={}
        with self.assertRaisesRegex(ValueError,'deployment ranges'):
            validate_config(config)
        config=self.config()
        config['endpoints'][0]['policy']={**fixtures.POLICY,'storage_verified':False}
        with self.assertRaisesRegex(ValueError,'storage rights'):
            validate_config(config)

    def test_external_schema_requires_review_hash_and_original_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for file in Path('vendor').glob('pump*.json'):
                (root/file.name).write_bytes(file.read_bytes())
            with self.assertRaisesRegex(ValueError,'review'):
                Decoder(schema_directory=root)
            for name in ('pump','pump_amm'):
                path=root/(name+'.manifest.json')
                value=json.loads(path.read_text())
                value.update(reviewed=True,evidence='synthetic test copy')
                path.write_text(json.dumps(value))
            self.assertEqual(len(Decoder(schema_directory=root).specs),2)
            (root/'pump.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                Decoder(schema_directory=root)

    def test_unreviewed_history_does_not_abort_eligible_training(self):
        from meme_learning import train_shared_model
        row={'entry_context':{'observed_at':1},'exit_signature':'eligible','exit_at':2,'net_pnl_usd':1}
        blocked=copy.deepcopy(row)
        blocked.update(exit_signature='quarantined',entry_context={'data_source_urls':['https://pump.fun/token']})
        model=train_shared_model(paper=[blocked,row])
        self.assertEqual(model['samples'],1)
        self.assertEqual(model['quarantined_samples'],{'bot_paper':1})

    def test_health_is_read_only_and_stale_index_cannot_be_ready(self):
        before=self.helper.path.read_bytes()
        report=health(self.helper.path,[fixtures.MINT],now=3700)
        self.assertFalse(report['ready'])
        self.assertEqual(before,self.helper.path.read_bytes())
        self.assertNotIn('secret',safe_reason(ValueError('failed https://user:secret@example.invalid/path')))

    def test_oracle_refresh_retains_missing_conversion_as_failure(self):
        from onchain_oracle import refresh
        from types import SimpleNamespace
        client=SimpleNamespace(source='fixture')
        config={'source_id':'fixture','feeds':[{'mint':fixtures.SOL}]}
        with patch('onchain_oracle.capture_sol',side_effect=ValueError('stale oracle')):
            report=refresh(self.helper.store,[client],config)
        self.assertEqual(report['status'],'incomplete')
        self.assertEqual(report['feeds'][0]['error'],'stale oracle')

    def test_repeated_oracle_publication_is_idempotent_across_capture_contexts(self):
        from test_onchain_hour import snapshot
        from onchain_oracle import import_sol
        self.helper.store.register_source('oracle-fixture',dict(fixtures.POLICY,
            oracle_data_rights_verified=True,oracle_max_confidence_bps=10000))
        value=snapshot()
        value['source_id']='oracle-fixture'
        import_sol(self.helper.store,value)
        second=copy.deepcopy(value)
        second['retrieved_at']+=1
        import_sol(self.helper.store,second)
        with self.helper.store.connect() as db:
            count=db.execute("SELECT count(*) FROM conversions WHERE source='oracle-fixture'").fetchone()[0]
        self.assertEqual(count,1)


if __name__=='__main__':
    unittest.main()
