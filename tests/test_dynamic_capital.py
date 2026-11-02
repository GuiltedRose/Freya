"""UNEXECUTED deterministic capital/retrieval and portfolio recovery contracts."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from capital_policy import CapitalConfig,CapitalPolicy,evidence_stats
from expectancy_validation import compare,compare_sizing


def outcomes(n=100):
    return [dict(id=str(i),group_id=str(i),eligible=True,exit_at=1000+i,
                 pnl=5 if i%10<3 else -1,net_return=5 if i%10<3 else -1) for i in range(n)]


class CapitalTests(unittest.TestCase):
    def policy(self,config=None):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        return CapitalPolicy(db,config or CapitalConfig())

    def test_low_win_rate_positive_asymmetry_has_positive_conservative_kelly(self):
        stats=evidence_stats(outcomes(),now=1100)
        self.assertLess(stats['win_probability'],.5)
        self.assertGreater(stats['expectancy_lower'],0)
        self.assertGreater(stats['kelly_fraction'],0)
        self.assertLessEqual(stats['kelly_fraction'],.25)

    def test_uncertain_negative_stale_and_duplicate_evidence_cannot_raise_risk(self):
        self.assertEqual(evidence_stats(outcomes(3),now=1100)['kelly_fraction'],0)
        negative=[dict(r,net_return=-1) for r in outcomes()]
        self.assertEqual(evidence_stats(negative,now=1100)['kelly_fraction'],0)
        self.assertEqual(evidence_stats(outcomes(),now=10_000_000)['kelly_fraction'],0)
        correlated=[dict(r,group_id='same') for r in outcomes()]
        self.assertEqual(evidence_stats(correlated,now=1100)['samples'],1)
        self.assertEqual(evidence_stats(outcomes()*2,now=1100)['samples'],100)

    def test_allocation_and_concurrency_promote_and_demote_independently(self):
        policy=self.policy(CapitalConfig(concurrency_promote_wins=12,concurrency_demote_losses=3))
        with policy.db:
            for i in range(5):policy.record(dict(outcomes(1)[0],id=str(i),group_id=str(i),exit_at=i))
        state=policy.state()
        self.assertEqual(state['allocation']['tier'],1)
        self.assertEqual(state['concurrency']['tier'],0)
        with policy.db:
            for i in range(5,7):policy.record(dict(outcomes(1)[0],id=str(i),group_id=str(i),pnl=-1,net_return=-1,exit_at=i))
        self.assertEqual(policy.state()['allocation']['tier'],0)
        self.assertEqual(policy.state()['concurrency']['losses'],2)
        other=self.policy(CapitalConfig(allocation_promote_wins=20,concurrency_promote_wins=2))
        with other.db:
            for row in outcomes(2):other.record(row)
        self.assertEqual(other.capacity(),2)
        self.assertEqual(other.state()['allocation']['tier'],0)

    def test_zero_selection_shared_exposure_and_capacity_twenty(self):
        policy=self.policy(CapitalConfig(concurrency_min=5))
        with policy.db:
            for row in outcomes():policy.record(row)
        sized=policy.size(1000,500,0,0,ml_fraction=.37,liquidity_limit=500,now=1100)
        self.assertGreater(sized['allocation_usd'],0)
        self.assertLessEqual(sized['allocation_usd'],sized['hard_maximum_usd'])
        self.assertEqual(policy.size(1000,500,0,0,ml_fraction=0,liquidity_limit=500,now=1100)['allocation_usd'],0)
        self.assertEqual(policy.size(1000,500,250,0,liquidity_limit=500,now=1100)['allocation_usd'],0)
        self.assertEqual(policy.size(1000,500,0,20,liquidity_limit=500,now=1100)['allocation_usd'],0)
        with self.assertRaises(ValueError):CapitalConfig(concurrency_steps=(1,21),concurrency_max=1)

    def test_restart_and_duplicate_close_do_not_repeat_tier_advancement(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'capital.sqlite'
            db=sqlite3.connect(path);policy=CapitalPolicy(db,CapitalConfig())
            with db:
                for row in outcomes():policy.record(row)
            saved=policy.state();db.close()
            db=sqlite3.connect(path)
            try:
                policy=CapitalPolicy(db,CapitalConfig())
                with db:policy.record(outcomes()[-1])
                self.assertEqual(policy.state(),saved)
                self.assertEqual(db.execute('SELECT count(*) FROM capital_outcomes').fetchone()[0],100)
                with self.assertRaises(ValueError):CapitalPolicy(db,CapitalConfig(concurrency_promote_wins=99))
            finally:db.close()

    def test_retrieval_reports_payoff_edge_despite_low_win_frequency(self):
        from unified_observations import ObservationStore,tensor
        from test_unified_learning import record
        with tempfile.TemporaryDirectory() as root:
            store=ObservationStore(Path(root)/'retrieval.sqlite',create=True)
            for i in range(60):store.add(record(i,net_pnl_usd=10 if i%3==0 else -1,cost_usd=10))
            result=store.retrieve(tensor(record()),10000,'none')
            self.assertLess(result['score'],.5)
            self.assertTrue(result['edge_supported'])
            self.assertGreater(result['expectancy_lower_usd'],0)

    def test_comparison_is_paired_grouped_and_drawdown_aware(self):
        rows=[dict(id=str(i),group_id=str(i),exit_at=i,pnl=5 if i%3==0 else -1,selected=i%3==0,probability=.34) for i in range(90)]
        report=compare(rows)
        self.assertEqual(report['status'],'promotable')
        self.assertLessEqual(report['policy_drawdown'],report['baseline_drawdown'])
        self.assertEqual(compare(rows*2)['groups'],90)
        inputs=[dict(r,entry_at=r['exit_at']-.5,cost_usd=10,equity_usd=100,fixed_cost_usd=.1,eligible=True,net_of_costs_verified=True) for r in rows]
        sized=compare_sizing(inputs,CapitalConfig())
        self.assertFalse(sized['live_eligible'])
        self.assertEqual(sized['status'],'modeled_only')

    def test_position_ownership_and_uncertain_order_survive_restart(self):
        from meme_live import MemeLiveStore,MemeLiveEngine
        from test_momentum_reclaim import policy as rules
        with tempfile.TemporaryDirectory() as root:
            store=MemeLiveStore(root,'user',rules())
            a=dict(mint='A',position_id='owned-A',entry_signature='sig-A',cost_usd=1)
            b=dict(mint='B',position_id='owned-B',entry_signature='sig-B',cost_usd=1)
            store.state.update(positions={'A':a,'B':b},position=a,position_owner='A',
                pending=dict(id='order-B',side='sell',mint='B',owner_position_id='owned-B'))
            store.save('test_fixture',{});store.close()
            store=MemeLiveStore(root,'user',rules())
            journal=sqlite3.connect(':memory:')
            journal.execute('CREATE TABLE orders(order_id TEXT,signature TEXT,state TEXT)')
            journal.execute("INSERT INTO orders VALUES('order-B','original-signature','uncertain')")
            orders=SimpleNamespace(db=journal,reconcile=lambda key:'uncertain')
            engine=MemeLiveEngine(None,'user',store,rules(),orders=orders,signer=object())
            try:
                with patch.object(engine,'resolve_expired',return_value='uncertain'):
                    self.assertEqual(engine.recover()['status'],'uncertain_order_blocks_replacement')
                self.assertEqual(len(store.all_positions()),2)
                self.assertEqual(store.state['pending']['id'],'order-B')
                store.state['pending']['owner_position_id']='owned-A'
                with self.assertRaisesRegex(ValueError,'ownership'):engine.recover()
                store.state['pending'].update(owner_position_id='owned-B',input_units=10,minimum=0,
                    sol_price_usd=100,base_account='base-B',quote_account='quote-B')
                orders.reconcile=lambda key:'confirmed'
                engine.rpc=SimpleNamespace(call=lambda *args:{'meta':{'err':None},'blockTime':1100,
                    'transaction':{'signatures':['original-signature']}})
                fill=dict(base_delta=-10,quote_delta=0,native_delta=2_000_000_000,fee_lamports=0,
                          own_account_rent_delta=0,net_cash_lamports=2_000_000_000)
                with patch('meme_live.receipt_fill',return_value=fill):
                    self.assertEqual(engine.recover()['status'],'confirmed_sell')
                self.assertEqual([p['mint'] for p in store.all_positions()],['A'])
                self.assertIsNone(store.state['pending'])
                self.assertEqual(engine.recover()['status'],'idle')
                self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE kind='confirmed_sell'").fetchone()[0],1)
            finally:journal.close();store.close()

    def test_policy_versions_and_revocation_preserve_history(self):
        policy=self.policy()
        with policy.db:
            for row in outcomes():policy.record(row)
            policy.revoke('99','corrected outcome')
        self.assertEqual(policy.state()['allocation']['tier'],0)
        self.assertEqual(policy.db.execute('SELECT count(*) FROM capital_outcomes').fetchone()[0],100)
        newer=CapitalPolicy(policy.db,CapitalConfig(version=2,allocation_promote_wins=7))
        self.assertEqual(newer.capacity(),1)
        self.assertEqual(policy.db.execute('SELECT count(*) FROM capital_policy').fetchone()[0],2)
        with self.assertRaises(ValueError):CapitalPolicy(policy.db,CapitalConfig())

    def test_paper_shared_bankroll_twenty_positions_restart_and_exit(self):
        import copy
        from paper_runtime import PaperSession
        from test_momentum_reclaim import policy as rules,setup
        config=CapitalConfig(concurrency_min=5)
        with tempfile.TemporaryDirectory() as root:
            session=PaperSession(root,0,rules(),100,100)
            try:
                session.capital_policy=CapitalPolicy(session.db,config)
                with session.db:
                    for row in outcomes():session.capital_policy.record(row)
                initial=session.trader.native_sol
                for i in range(20):
                    row=copy.deepcopy(setup())
                    row.update(mint='Mint'+str(i),pool='Pool'+str(i),observed_at=1200+i)
                    row['liquidity_usd']=1_000_000
                    row['candle_history'].update(mint=row['mint'],pool=row['pool'])
                    row['execution'].update(mint=row['mint'],pool=row['pool'],observed_at=row['observed_at'])
                    self.assertEqual(session.process(row)['action'],'paper_buy')
                self.assertEqual(len(session.trader.all_positions()),20)
                self.assertLess(session.trader.native_sol,initial)
                self.assertGreaterEqual(session.trader.native_sol,rules().native_fee_reserve_sol)
                extra=copy.deepcopy(row);extra.update(mint='Overflow',pool='Overflow',observed_at=1220)
                before=session.trader.native_sol
                self.assertEqual(session.process(extra)['action'],'hold')
                self.assertEqual(session.trader.native_sol,before)
            finally:session.close()
            session=PaperSession(root)
            try:
                session.capital_policy=CapitalPolicy(session.db,config)
                self.assertEqual(len(session.trader.all_positions()),20)
                row=copy.deepcopy(setup());row.update(mint='Mint0',pool='Pool0',observed_at=1300,price_usd=.1)
                row['execution'].update(mint='Mint0',pool='Pool0',observed_at=1300)
                self.assertEqual(session.process(row)['action'],'paper_sell')
                self.assertEqual(len(session.trader.all_positions()),19)
            finally:session.close()
