"""Unexecuted deterministic contracts. Fixtures never qualify a live model."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from market_analysis import lifecycle,risk_features,risk_tensor,risk_schema,verified_risk_label
from execution_liquidity import cap_notional,cap_native
from paper_challengers import ChallengerConfig,PaperManagement
from paper_runtime import encode_state,decode_state
from meme_dip import PaperDipTrader,DipRules
from challenger_registry import Registry
from challenger_lab import Lab
from runtime_health import validate_rpc_mode


def snapshot(now=1200):
    bars=[dict(timestamp=t,open=1.,high=1.01,low=.99,close=1.,volume_usd=10.,final=True)
          for t in (now-180,now-120,now-60)]
    return dict(mint='mint',pool='pool',observed_at=now,price_usd=1.,
                candle_history=dict(mint='mint',pool='pool',candles=bars))


class ContextTests(unittest.TestCase):
    def test_context_is_not_a_risk_label_and_creation_is_not_inferred(self):
        sample=snapshot()
        self.assertEqual(lifecycle(sample)['regime'],'consolidation')
        self.assertIsNone(lifecycle(sample)['risk_label'])
        self.assertIsNone(lifecycle(sample)['age_seconds'])
        sample['candle_history']['candles'][0]['timestamp']+=1
        self.assertEqual(lifecycle(sample)['regime'],'unknown')
        self.assertEqual(lifecycle(dict(observed_at=None,candle_history=[]))['regime'],'unknown')

    def test_missing_stale_and_foreign_risk_evidence_never_becomes_safe(self):
        sample=snapshot();self.assertTrue(all(v is None for v in risk_features(sample)))
        sample['risk_evidence']=dict(mint='other',pool='pool',observed_at=1200,verified=True,coverage=1,evidence='fixture')
        self.assertTrue(all(v is None for v in risk_features(sample)))
        with self.assertRaises(ValueError):risk_tensor(dict(risk_features=[None]*12,risk_schema=risk_schema()))
        self.assertFalse(verified_risk_label(dict(risk_label=1,risk_label_verified=True,feature_at=1,risk_label_at=2,
            risk_label_evidence=dict(reviewed=True,evidence='fixture',kind='rug_pull',basis='price_loss'))))

    def test_liquidity_can_only_reduce_notional(self):
        self.assertEqual(cap_notional(100,0)['maximum_usd'],0)
        self.assertLessEqual(cap_notional(100,10000)['maximum_usd'],10)
        self.assertEqual(cap_notional(100,10000,slippage=.01)['maximum_usd'],0)
        adapter=SimpleNamespace(output=lambda side,amount,market:amount)
        self.assertLessEqual(cap_native(adapter,dict(quote=10000,base=10000,fee_bps=0),100),10)
        adapter.output=lambda *args:0
        self.assertEqual(cap_native(adapter,dict(quote=10000,base=10000,fee_bps=0),100),0)

    def test_rpc_mode_is_explicit_without_granting_permissions(self):
        local=dict(enabled=True,url='http://127.0.0.1:8899')
        remote=dict(enabled=True,url='https://rpc.example.invalid')
        validate_rpc_mode(dict(endpoints=[local,remote]),'hybrid')
        with self.assertRaises(ValueError):validate_rpc_mode(dict(endpoints=[remote]),'selfhosted')
        with self.assertRaises(ValueError):validate_rpc_mode(dict(endpoints=[local]),'remote')
        self.assertNotIn('policy',local)

    def test_continuation_windows_do_not_confuse_changing_targets_with_trade_labels(self):
        from unified_learning import windows,metrics,MAX_STEPS
        from test_unified_learning import record
        rows=[record(i,position_id='one',group_id='one',net_pnl_usd=1,
            continuation_label=i%2,continuation_known_at=i*100+80) for i in range(MAX_STEPS*2)]
        sequences=windows(rows,continuation=True)
        self.assertEqual(len(sequences),2)
        self.assertNotEqual(sequences[0][2]['position_id'],sequences[1][2]['position_id'])
        self.assertIsNone(sequences[0][2]['net_pnl_usd'])
        self.assertEqual(metrics([(.5,label,row) for _,label,row in sequences])['positions'],2)

    def test_continuation_calibration_is_applied_without_becoming_a_kelly_probability(self):
        from shadow_ml_models import initialize_lstm,predict
        model=initialize_lstm(1);model['probability_calibration']=[0.,0.]
        self.assertEqual(predict(model,[[.1],[.2]]),.5)
        self.assertNotIn('kelly_fraction',model)


class ManagementTests(unittest.TestCase):
    def held(self):
        rules=DipRules(entry_style='volume_candles',momentum_reclaim_enabled=True)
        trader=PaperDipTrader(100,rules)
        trader.cash=90
        trader.position=dict(mint='mint',pool='pool',quantity=10.,cash_spent=10.,entry_price=1.,
            stop=.8,target=9.,opened_at=1000,entry_style='volume_candles',exit_fixed_cost=.001,
            entry_native_cost_usd=0.,take_profit_usd=99.,max_loss_usd=2.)
        trader.challenger_config=asdict(ChallengerConfig(partial_profits=True))
        trader.paper_management=PaperManagement(ChallengerConfig(partial_profits=True))
        return trader

    def test_partial_restart_full_exit_counts_all_costs_once(self):
        trader=self.held()
        first=trader.on_snapshot(dict(snapshot(),price_usd=1.4))
        self.assertEqual(first['action'],'paper_partial_sell')
        self.assertAlmostEqual(trader.position['quantity'],7.5)
        self.assertEqual(trader.consecutive_losses,0)
        restored=decode_state(json.loads(json.dumps(encode_state(trader))))
        last=restored.on_snapshot(dict(snapshot(1260),price_usd=.5))
        self.assertEqual(last['action'],'paper_sell')
        self.assertIsNone(restored.position)
        total=first['pnl_usd']+last['pnl_usd']
        self.assertAlmostEqual(last['trade_pnl_usd'],total)
        self.assertAlmostEqual(restored.cash-100,total)
        self.assertAlmostEqual(restored.strategy_realized_pnl_usd,total)
        self.assertEqual(restored.consecutive_losses,1)

    def test_stop_does_not_wait_for_models_or_challenger_profit(self):
        trader=self.held();trader.capital_error='ML unavailable'
        event=trader.on_snapshot(dict(snapshot(),price_usd=.5))
        self.assertEqual(event['action'],'paper_sell')
        self.assertEqual(event['sold_fraction'],1)

    def test_independent_flags_churn_and_hold_bound(self):
        cfg=ChallengerConfig(reentry=True,adaptive_holding=True)
        manager=PaperManagement(cfg);trader=SimpleNamespace()
        sample=snapshot();manager.entered(trader,sample);manager.closed(trader,sample)
        self.assertFalse(manager.entry_allowed(trader,snapshot(1260)))
        self.assertTrue(manager.entry_allowed(trader,snapshot(1560)))
        position={};duration=manager.holding_seconds(position,sample,900)
        self.assertEqual(manager.holding_seconds(position,snapshot(1800),900),duration)
        self.assertFalse(cfg.scaling);self.assertFalse(cfg.partial_profits)
        with self.assertRaises(ValueError):ChallengerConfig(partial_fractions=(float('nan'),))


class PersistenceTests(unittest.TestCase):
    def test_compressed_history_cannot_expand_past_the_observation_budget(self):
        import zstandard
        from challenger_lab import decode_snapshot,MAX_SNAPSHOT
        payload=zstandard.ZstdCompressor().compress(b' '*(MAX_SNAPSHOT*2))
        with self.assertRaises(ValueError):decode_snapshot(payload)

    def test_lab_is_isolated_immutable_and_crashed_leases_expire(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lab.sqlite';lab=Lab(path,create=True)
            rules=asdict(DipRules(entry_style='volume_candles',momentum_reclaim_enabled=True))
            spec=dict(rules=rules,initial=dict(balance=100),capital={},start_at=1,challengers={})
            lab.register('baseline',spec)
            with self.assertRaises(ValueError):lab.register('baseline',dict(spec,start_at=2))
            with patch('meme_learning.decision_context',return_value={'data_rights':{'learning_allowed':True}}):
                lab.append(snapshot(),DipRules(**rules))
                lab.append(snapshot(),DipRules(**rules))
            self.assertEqual(lab.db.execute('SELECT count(*) FROM history').fetchone()[0],1)
            first,token=lab.claim();self.assertEqual(first[0],'baseline')
            lab.close();lab=Lab(path)
            try:
                self.assertIsNone(lab.claim()[0])
                with lab.db:lab.db.execute('UPDATE scenarios SET expires=0')
                reclaimed,new_token=lab.claim()
                self.assertEqual(reclaimed[0],'baseline');self.assertNotEqual(token,new_token)
                self.assertEqual(reclaimed[2],0)
                self.assertEqual(lab.db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            finally:lab.close()

    def test_corrupt_history_pauses_without_advancing_or_committing_a_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            lab=Lab(Path(directory)/'lab.sqlite',create=True)
            try:
                rules=DipRules(entry_style='volume_candles',momentum_reclaim_enabled=True)
                lab.register('baseline',dict(rules=asdict(rules),initial=dict(balance=100),capital={},start_at=1,challengers={}))
                with patch('meme_learning.decision_context',return_value={'data_rights':{'learning_allowed':True}}):
                    lab.append(snapshot(),rules)
                with lab.db:lab.db.execute("UPDATE history SET hash='corrupt'")
                self.assertEqual(lab.batch(),-1)
                self.assertEqual(lab.db.execute('SELECT cursor,status,lease FROM scenarios').fetchone(),(0,'paused',None))
                self.assertEqual(lab.db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            finally:lab.close()

    def test_synthetic_or_degraded_outcomes_cannot_promote(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        registry=Registry(db)
        rows=[dict(id=str(i),group_id=str(i),family='target',candidate='a',synthetic=True,
            entry_at=100+i*86400,exit_at=200+i*86400,prediction_at=1,policy_frozen_at=1,
            baseline_matched=True,risk_bypass=False,entry_signal='momentum_reclaim',
            net_of_costs_verified=True,baseline_pnl=1.,candidate_pnl=3.) for i in range(40)]
        report=registry.evaluate('target','a',rows)
        self.assertEqual(report['status'],'collecting');self.assertFalse(report['live_eligible'])
        with self.assertRaises(ValueError):registry.transition('target','a',report['evaluation_id'],'promote')
        degraded=registry.evaluate('target','a',[dict(r,synthetic=False,candidate_pnl=-3.) for r in rows])
        self.assertEqual(degraded['status'],'rejected')

    def test_disk_guard_never_deletes_to_make_space(self):
        from node_storage_guard import check
        with patch('node_storage_guard.os.statvfs',return_value=SimpleNamespace(f_bavail=1,f_frsize=4096)):
            with self.assertRaises(RuntimeError):check(['/node'],8192)
            check(['/node'],4096)


if __name__=='__main__':unittest.main()
