"""UNEXECUTED pipeline contracts. No services or external requests are launched."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from config import public_env
from data_policy import SourceAccessBlocked
from onchain_store import EventStore
from pipeline_runtime import attached,check_index_target,preflight,resolve_indexer,writer_lock
from run_onchain_indexer import bootstrap_start
from run_bot_pipeline import clean_environment,prepare_files,supervise


class ConfigurationTests(unittest.TestCase):
    def test_existing_paper_session_enters_setup_and_resumes_without_initialize(self):
        from run_bot_pipeline import main
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'paper';state.mkdir()
            saved=b'preserved session fixture';(state/'paper.sqlite').write_bytes(saved)
            indexer=root/'indexer.json';learning=root/'learning.json'
            def setup(args,public,*templates):
                indexer.write_text(json.dumps({'oracle':{'feeds':[{}]}}))
                learning.write_text(json.dumps({'ledgers':[]}))
            command=['run_bot_pipeline.py','--state-dir',str(state),'--env-file',str(root/'absent.env'),
                '--indexer-config',str(indexer),'--learning-config',str(learning),
                '--onchain-db',str(root/'index.sqlite'),'--unified-db',str(root/'models.sqlite')]
            with patch('sys.argv',command),patch('sys.stdout',io.StringIO()),patch('sys.stdin.isatty',return_value=True),patch.dict(os.environ,{},clear=True),patch('pipeline_setup.configure',side_effect=setup) as configure,patch('run_bot_pipeline.preflight',return_value=[]),patch('capital_policy.load_config'),patch('run_bot_pipeline.supervise') as supervise:
                main()
            configure.assert_called_once()
            supervise.assert_called_once()
            self.assertFalse(supervise.call_args.args[0].initialize)
            self.assertEqual((state/'paper.sqlite').read_bytes(),saved)

    def test_initialize_creates_missing_config_without_starting_workers_or_claiming_review(self):
        from run_bot_pipeline import main
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=io.StringIO()
            command=['run_bot_pipeline.py','--initialize','--env-file',str(root/'absent.env'),
                '--indexer-config',str(root/'indexer.json'),'--learning-config',str(root/'learning.json'),
                '--state-dir',str(root/'paper')]
            with patch('sys.argv',command),patch('sys.stdout',output),patch('sys.stdin.isatty',return_value=False),patch('run_bot_pipeline.supervise') as supervise,patch.dict(os.environ,{},clear=True):
                with self.assertRaises(SystemExit) as error:main()
            self.assertEqual(error.exception.code,2)
            result=json.loads(output.getvalue())
            self.assertEqual(result['status'],'configuration_required')
            self.assertFalse(result['services_started'])
            self.assertTrue((root/'indexer.json').is_file())
            self.assertTrue((root/'learning.json').is_file())
            self.assertFalse(json.loads((root/'indexer.json').read_text())['endpoints'][0]['enabled'])
            self.assertFalse((root/'paper').exists())
            supervise.assert_not_called()

    def test_index_target_cannot_repurpose_existing_trading_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'paper.sqlite'
            with sqlite3.connect(path) as db:db.execute('CREATE TABLE paper_runs(id INTEGER)')
            with self.assertRaises(ValueError):check_index_target(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('paper_runs',)])

    def test_collection_environment_never_imports_signing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            path.write_text('SOLANA_RPC_URL=https://rpc.example.invalid\nSOLANA_PRIVATE_KEY=PRIVATE_FIXTURE\nSOLANA_KEYPAIR_PATH=/private/fixture\n')
            path.chmod(0o600)
            with patch.dict(os.environ,{},clear=True):
                result=public_env(path)
                self.assertEqual(result,{'SOLANA_RPC_URL':'https://rpc.example.invalid'})
                self.assertNotIn('SOLANA_PRIVATE_KEY',os.environ)
            with patch.dict(os.environ,{'SOLANA_PRIVATE_KEY':'PRIVATE_FIXTURE','SOLANA_KEYPAIR_PATH':'/fixture'}):
                self.assertNotIn('SOLANA_PRIVATE_KEY',clean_environment())
                self.assertNotIn('SOLANA_KEYPAIR_PATH',clean_environment())

    def test_environment_endpoint_requires_explicit_host_binding_without_approving_rights(self):
        config={'endpoints':[{'url':'${SOLANA_RPC_URL}','enabled':True,
                 'policy':{'allowed_rpc_hosts':['rpc.example.invalid'],'storage_verified':False}}]}
        resolved=resolve_indexer(config,{'SOLANA_RPC_URL':'https://rpc.example.invalid'})
        self.assertEqual(resolved['endpoints'][0]['url'],'https://rpc.example.invalid')
        self.assertFalse(resolved['endpoints'][0]['policy']['storage_verified'])
        self.assertEqual(config['endpoints'][0]['url'],'${SOLANA_RPC_URL}')
        with self.assertRaises(ValueError):resolve_indexer(config,{'SOLANA_RPC_URL':'https://unapproved.example.invalid'})

    def test_aggregate_preflight_reports_independent_missing_requirements(self):
        with patch('runtime_health.validate_config',side_effect=ValueError('Deployment ranges missing')):
            issues=preflight({'endpoints':[{'enabled':True,'policy':{}}]}, {'rights':{},'provenance':{},'ledgers':[]},ledger_paths=['paper.sqlite'])
        self.assertGreater(len(issues),8)
        self.assertTrue(any('reviewed_program_versions' in issue for issue in issues))
        self.assertTrue(any('oracle.feeds' in issue for issue in issues))
        self.assertTrue(any('learning.ledgers' in issue for issue in issues))

    def test_review_template_creation_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=SimpleNamespace(indexer_config=root/'indexer.json',learning_config=root/'learning.json',
                                 paper_state=root/'paper',live_state=root/'live')
            with patch('sys.stdout',io.StringIO()):prepare_files(args)
            config=json.loads(args.indexer_config.read_text())
            self.assertFalse(config['endpoints'][0]['enabled'])
            self.assertFalse(config['endpoints'][0]['policy']['storage_verified'])
            for rows in config['reviewed_program_versions'].values():self.assertIsNone(rows[0]['deployment_slot'])
            args.indexer_config.write_text('{"operator":"preserve"}')
            with patch('sys.stdout',io.StringIO()):prepare_files(args)
            self.assertEqual(json.loads(args.indexer_config.read_text()),{'operator':'preserve'})


class OwnershipTests(unittest.TestCase):
    def test_both_consumers_receive_shared_data_paths_and_stop_only_owned_children(self):
        from unified_observations import ObservationStore
        for mode in ('paper','live'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);index=root/'index.sqlite';models=root/'models.sqlite'
                EventStore(index);ObservationStore(models,create=True)
                (root/'capital.json').write_text('{}')
                args=SimpleNamespace(mode=mode,paper_state=root/'paper',live_state=root/'live',
                    onchain_db=index,unified_db=models,external_indexer=False,external_workers=False,
                    capital_config=root/'capital.json',learning_interval=900,initialize=mode=='paper',
                    execute=mode=='live',manage_only=False,env_file=root/'.env')
                launched=[];stopped=[]
                def start(command,**options):
                    child=MagicMock(pid=1000+len(launched));child.poll.return_value=None
                    launched.append((command,options,child));return child
                with patch.dict(os.environ,{'SOLANA_PRIVATE_KEY':'PRIVATE_FIXTURE'}),patch('run_bot_pipeline.attached',return_value=False),patch('run_bot_pipeline.subprocess.Popen',side_effect=start),patch('run_bot_pipeline.stop',side_effect=lambda child:stopped.append(child)),patch('run_bot_pipeline.monitor'),patch('sys.stdout',io.StringIO()):
                    supervise(args,[],{'endpoints':[]},{'ledgers':[]})
                self.assertEqual(len(launched),3)
                for _,options,_ in launched[:2]:self.assertNotIn('SOLANA_PRIVATE_KEY',options['env'])
                consumer=launched[2]
                self.assertIn(str(index),consumer[0]);self.assertIn(str(models),consumer[0])
                self.assertEqual('--execute' in consumer[0],mode=='live')
                self.assertEqual('--no-auto-reset' in consumer[0],mode=='paper')
                if mode=='paper':self.assertNotIn('SOLANA_PRIVATE_KEY',consumer[1]['env'])
                self.assertEqual(stopped,[launched[2][2],launched[1][2],launched[0][2]])
                self.assertTrue((root/mode/'pipeline'/mode/'status.json').is_file())

    def test_live_and_paper_can_verify_independently_owned_worker_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'index.sqlite';config={'source':'fixture'}
            with writer_lock(database,'indexer',config):
                self.assertTrue(attached(database,'indexer',config))
                with self.assertRaises(ValueError):attached(database,'indexer',{'source':'changed'})
                with self.assertRaises(ValueError):
                    with writer_lock(database,'indexer',config):pass
            self.assertFalse(attached(database,'indexer',config))

    def test_fresh_genesis_cursor_is_saved_once_and_existing_cursor_is_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            store=EventStore(Path(directory)/'index.sqlite')
            rpc=MagicMock();rpc.call.return_value=71
            self.assertEqual(bootstrap_start(store,[rpc]),0)
            self.assertEqual(store.next_slot(),0)
            self.assertIsNone(bootstrap_start(store,[rpc],999))
            rpc.call.assert_not_called()

    def test_permission_denial_cannot_fail_over_to_another_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store=EventStore(Path(directory)/'index.sqlite')
            from onchain_ingest import Ingester
            first=MagicMock(source='first');first.validate.side_effect=SourceAccessBlocked('fixture denied')
            backup=MagicMock(source='backup')
            for name in ('first','backup'):
                store.register_source(name,{'storage_verified':True,'evidence':'offline fixture'})
            bootstrap_start(store,[first,backup])
            with self.assertRaises(SourceAccessBlocked):
                Ingester(store,[first,backup],decoder=SimpleNamespace(specs={})).step()
            backup.validate.assert_not_called()
            self.assertEqual(store.next_slot(),0)


class LiveManagementTests(unittest.TestCase):
    def test_live_needs_no_paper_history_but_preserves_drawdown_and_source_vetoes(self):
        from meme_live import MemeLiveEngine
        engine=MemeLiveEngine.__new__(MemeLiveEngine)
        engine.signer=object();engine.mark=MagicMock()
        engine.store=SimpleNamespace(state={'pending':None,'position':None,'halted':True},select_position=MagicMock())
        engine.eligibility_provider=MagicMock(side_effect=AssertionError('Paper promotions must not gate live'))
        snapshot={'mint':'fixture','observed_at':100,'execution':{}}
        self.assertEqual(engine.prepare(snapshot,100,10,now=100)['status'],'drawdown_halted')
        engine.eligibility_provider.assert_not_called()
        engine.requires_source_qualification=True
        self.assertEqual(engine.prepare(snapshot,100,10,now=100)['status'],'market_source_unqualified')

    def test_learning_refresh_failure_still_services_existing_position(self):
        from run_meme_live import tick
        position={'mint':'fixture','pool':'pool','kind':'pumpswap'}
        store=SimpleNamespace(state={'pending':None},all_positions=lambda:[position],select_position=MagicMock(),
                              refresh_learning_model=MagicMock(side_effect=ValueError('unavailable')))
        engine=SimpleNamespace(store=store,rpc=MagicMock(),user='fixture',loss_streak_cooldown=lambda now:None,
            prepare=MagicMock(return_value={'status':'held_position_managed'}))
        sources=SimpleNamespace(observation=lambda mint:dict(position,market_kind='pumpswap'))
        with patch('run_meme_live.valuation',return_value=100),patch('run_meme_live.wallet_equity',return_value=10):
            result=tick(engine,sources,None,[],[])
        engine.prepare.assert_called_once()
        self.assertEqual(result['status'],'learning_unavailable_entries_blocked')
        self.assertEqual(result['positions_retained'],1)

    def test_optional_exit_model_failure_does_not_raise_past_hard_exit_controls(self):
        from neural_exits import decision
        with patch('neural_exits._decision',side_effect=ValueError('invalid model fixture')):
            self.assertIsNone(decision({}, {},None,0,1))

    def test_index_failure_and_unavailable_ml_still_attempt_held_position_recovery(self):
        from run_meme_live import tick
        position={'mint':'fixture','pool':'pool','kind':'pumpswap'}
        row=dict(position,market_kind='pumpswap',exit_only=True,recovery_sol_price_usd=101)
        store=SimpleNamespace(state={'pending':None},all_positions=lambda:[position],select_position=MagicMock(),
            refresh_learning_model=MagicMock(side_effect=ValueError('no ML')))
        engine=SimpleNamespace(store=store,rpc=MagicMock(),user='fixture',loss_streak_cooldown=lambda now:None,
            prepare=MagicMock(return_value={'status':'holding'}))
        sources=SimpleNamespace(observation=MagicMock(side_effect=ValueError('index unavailable')),
            position_observation=MagicMock(return_value=row))
        with patch('run_meme_live.valuation',return_value=100),patch('run_meme_live.wallet_equity',return_value=10):
            report=tick(engine,sources,None,[],[])
        sources.position_observation.assert_called_once_with(engine,position)
        engine.prepare.assert_called_once_with(row,101,10)
        self.assertEqual(report['status'],'learning_unavailable_entries_blocked')

    def test_live_mode_requires_explicit_execution_flag_before_configuration_or_wallet_loading(self):
        from run_bot_pipeline import main
        with tempfile.TemporaryDirectory() as directory:
            with patch('sys.argv',['run_bot_pipeline.py','--mode','live','--env-file',str(Path(directory)/'absent')]),patch('sys.stderr',io.StringIO()),patch('wallet.load_wallet') as wallet:
                with self.assertRaises(SystemExit) as error:main()
                self.assertEqual(error.exception.code,2)
                wallet.assert_not_called()


class PercentageWarmupTests(unittest.TestCase):
    def policy(self,paper):
        import sqlite3
        from capital_policy import CapitalConfig,CapitalPolicy
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        return CapitalPolicy(db,CapitalConfig(),paper=paper)

    def test_fresh_percentage_sizing_and_optional_strict_kelly(self):
        paper=self.policy(True)
        decision=paper.size(100,4,0,0,liquidity_limit=100,now=100)
        self.assertEqual(decision['allocation_usd'],1)
        self.assertEqual(decision['sizing_mode'],'paper_percentage_warmup')
        self.assertEqual(decision['kelly_ceiling_usd'],0)
        self.assertEqual(self.policy(False).size(100,4,0,0,liquidity_limit=100,now=100)['allocation_usd'],0)
        self.assertEqual(paper.db.execute('SELECT count(*) FROM capital_decisions').fetchone()[0],1)

    def test_negative_evidence_zero_choice_and_shared_capacity_remain_vetoes(self):
        paper=self.policy(True)
        self.assertEqual(paper.size(100,4,0,0,ml_fraction=0,liquidity_limit=100,now=100)['allocation_usd'],0)
        self.assertEqual(paper.size(100,4,25,0,liquidity_limit=100,now=100)['allocation_usd'],0)
        self.assertEqual(paper.size(100,4,0,1,liquidity_limit=100,now=100)['allocation_usd'],0)
        row=dict(id='loss',group_id='mint',exit_at=99,eligible=True,net_return=-.1,pnl=-1)
        paper.db.execute('INSERT INTO capital_outcomes VALUES(?,?,?)',('loss',99,json.dumps(row)))
        self.assertEqual(paper.size(100,4,0,0,liquidity_limit=100,now=100)['allocation_usd'],0)

    def test_warmup_restart_preserves_tiers_and_strict_policy(self):
        import sqlite3
        from capital_policy import CapitalConfig,CapitalPolicy
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'capital.sqlite';config=CapitalConfig()
            with sqlite3.connect(path) as db:
                first=CapitalPolicy(db,config,paper=True)
                decision=first.size(100,4,0,0,liquidity_limit=100,now=100)
            with sqlite3.connect(path) as db:
                restored=CapitalPolicy(db,config,paper=True)
                self.assertEqual(restored.state(),decision['tiers'])
                self.assertEqual(restored.size(100,4,0,0,liquidity_limit=100,now=100)['allocation_usd'],1)
                live=CapitalPolicy(db,config)
                self.assertEqual(live.size(100,4,0,0,liquidity_limit=100,now=100)['allocation_usd'],0)
                live_learning=CapitalPolicy(db,config,percentage_warmup=True)
                sizing=live_learning.size(100,4,0,0,liquidity_limit=100,now=100)
                self.assertEqual(sizing['allocation_usd'],1)
                self.assertEqual(sizing['sizing_mode'],'live_percentage_warmup')
                self.assertEqual(sizing['kelly_ceiling_usd'],0)
