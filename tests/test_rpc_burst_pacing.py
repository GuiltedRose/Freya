"""Offline shared-provider pacing regressions; no network or real sleeps."""
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from provider_limits import ProviderBudget, ProviderBackoff
from data_policy import SourceAccessBlocked


class RpcBurstPacingTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.now=1000.
        self.path=Path(self.root.name)/'budget.sqlite'
        self.budget=self.client()
        self.url='https://api.mainnet-beta.solana.com'

    def client(self):
        return ProviderBudget(self.path,clock=lambda:self.now,sleep=self.advance)

    def advance(self,seconds):
        self.now+=seconds

    def test_planner_reports_shared_backoff_without_http_retry(self):
        from unittest.mock import patch
        from onchain_ingest import IndexRpc
        rpc=IndexRpc({'url':self.url,'source_id':'fixture'},self.budget)
        events=[]
        rpc._planning_observer=lambda phase, metrics: events.append((phase,metrics))
        for _ in range(10):self.budget.reserve_rpc(self.url)
        with patch('onchain_ingest.time.monotonic',side_effect=lambda:self.now):
            rpc._reserve('getSlotLeaders')
        self.assertIn('provider_backoff',[phase for phase,_ in events])
        phase,metrics=events[-1]
        self.assertEqual(phase,'provider_reservation_finished')
        self.assertGreaterEqual(metrics['backoff_sleep_seconds'],29)
        self.assertGreaterEqual(metrics['reservation_elapsed_ms'],29000)
        self.assertEqual(metrics['reservation_attempts'],2)
        self.assertEqual(rpc.requests,0)
        self.assertNotIn(self.url,str(events))

    def test_shared_aliases_and_restart_preserve_burst(self):
        for _ in range(10):self.budget.reserve_rpc(self.url)
        other=self.client()
        with self.assertRaises(ProviderBackoff):
            other.reserve_rpc('https://api.mainnet.solana.com')
        remaining=other.cooldown_remaining(self.url)
        self.assertGreaterEqual(remaining,10)
        self.advance(remaining)
        other.reserve_rpc(self.url)
        self.assertEqual(other.cooldown_remaining(self.url),0)

    def test_custom_external_clients_share_allowance(self):
        url='https://reviewed-provider.example/rpc'
        for _ in range(5):
            self.budget.reserve_rpc(url)
            self.client().reserve_rpc(url)
        with self.assertRaises(ProviderBackoff):self.client().reserve_rpc(url)

    def test_success_cannot_clear_pause_and_retry_after_wins(self):
        for _ in range(10):self.budget.reserve_rpc(self.url)
        self.budget.success(self.url)
        with self.assertRaises(ProviderBackoff):self.budget.reserve_rpc(self.url)
        self.budget.failure(self.url,429,{'Retry-After':'120'})
        self.advance(11)
        with self.assertRaises(ProviderBackoff) as error:self.budget.reserve_rpc(self.url)
        self.assertGreater(error.exception.seconds,100)
        self.advance(120)
        self.budget.failure(self.url,403)
        with self.assertRaises(SourceAccessBlocked):self.budget.reserve_rpc(self.url)

    def test_no_extra_burst_pause_for_local_node(self):
        for _ in range(12):self.budget.reserve_rpc('https://127.0.0.1')
        self.assertEqual(self.budget.cooldown_remaining('https://127.0.0.1'),0)

    def test_invalid_policy_does_not_reserve(self):
        for count in (0,True,101):
            with self.assertRaises(ValueError):
                self.budget.reserve(self.url,burst_requests=count)
        for seconds in (0,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                self.budget.reserve(self.url,burst_requests=10,burst_cooldown_seconds=seconds)

    def test_concurrent_workers_cannot_each_claim_a_full_burst(self):
        def reserve(_):
            budget=ProviderBudget(self.path,clock=lambda:1000.,sleep=lambda seconds:None)
            try:budget.reserve_rpc(self.url)
            except ProviderBackoff:return False
            return True
        # Initialize schema before racing separate process-equivalent connections.
        self.budget.connect().close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted=list(pool.map(reserve,range(16)))
        self.assertEqual(sum(accepted),10)
