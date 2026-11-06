"""UNEXECUTED controller regression tests; all OS calls mocked."""
import unittest
from unittest.mock import patch, MagicMock
from solana_network_lifecycle import host_rules, cleanup, start, controller_stop_reason, close_session


class LifecycleTests(unittest.TestCase):
    def test_node_peers_are_opt_in_and_private_rpc_is_not_forwarded(self):
        self.assertNotIn('dnat to',host_rules('wan0'))
        rules=host_rules('wan0',{'start':8000,'end':8020})
        self.assertIn('tcp dport 8000-8020 dnat to 169.254.203.2',rules)
        self.assertIn('udp dport 8000-8020 dnat to 169.254.203.2',rules)
        self.assertNotIn('8899',rules)
        for ports in ({'start':8899,'end':8900},{'start':1,'end':22},
                      {'start':8000,'end':9000},{'start':8020,'end':8000}):
            with self.assertRaises(ValueError):host_rules('wan0',ports)

    def test_host_guard_cannot_be_injected_and_blocks_host_proxy_paths(self):
        for value in ('eth0; flush ruleset','', 'a'*16):
            with self.assertRaises(ValueError): host_rules(value)
        rules=host_rules('wan0')
        self.assertIn('iifname "solana-host" drop',rules)
        self.assertIn('oifname "solana-host" drop',rules)
        self.assertIn('iifname "solana-host" oifname != "wan0" drop',rules)
        self.assertIn('meta nfproto ipv6 drop',rules)
        self.assertNotIn('flush ruleset',rules)

    def test_unowned_namespace_is_not_removed(self):
        with patch('solana_network_lifecycle.STATUS'), patch('solana_network_lifecycle.OWNED') as owned, patch('solana_network_lifecycle.run') as run:
            owned.exists.return_value=False
            cleanup()
            run.assert_not_called()

    def test_missing_accounting_prevents_any_network_setup(self):
        with patch('solana_network_lifecycle.Path') as path, patch('solana_network_lifecycle.run') as run:
            path.return_value.is_symlink.return_value=False
            path.return_value.stat.side_effect=FileNotFoundError('missing ledger')
            with self.assertRaises(FileNotFoundError):
                start({'ledger':'/private/missing.sqlite'})
            run.assert_not_called()

    def test_cleanup_disables_link_before_destroying_namespace(self):
        with patch('solana_network_lifecycle.STATUS'), patch('solana_network_lifecycle.OWNED') as owned, patch('solana_network_lifecycle.run') as run:
            owned.exists.return_value=True
            cleanup()
            calls=[call.args for call in run.call_args_list]
            self.assertEqual(calls[0],('ip','link','set','solana-host','down'))
            self.assertEqual(calls[-1],('ip','netns','del','solana-budget'))

    def test_stop_reason_does_not_extend_or_renew_lease(self):
        self.assertEqual(controller_stop_reason(False, 155, 155), 'lease_expired')
        self.assertEqual(controller_stop_reason(True, 154, 155), 'stop_requested')
        self.assertEqual(controller_stop_reason(False, 154, 155, 1000, 1000), 'grant_exhausted')
        self.assertIsNone(controller_stop_reason(False, 154, 155, 999, 1000))

    def test_audit_failure_still_samples_and_cleans_up(self):
        with patch('solana_network_lifecycle.close_boundary') as boundary, \
             patch('solana_network_lifecycle.sample_os') as sample, \
             patch('solana_network_lifecycle.cleanup') as clean:
            event=MagicMock(side_effect=OSError('private diagnostic failure'))
            budget=object()
            with self.assertRaises(OSError): close_session(budget, 'grant', event)
            boundary.assert_called_once()
            sample.assert_called_once()
            clean.assert_called_once()

    def test_boundary_failure_still_attempts_cleanup(self):
        with patch('solana_network_lifecycle.close_boundary', side_effect=OSError()), \
             patch('solana_network_lifecycle.sample_os') as sample, \
             patch('solana_network_lifecycle.cleanup') as clean:
            with self.assertRaises(OSError): close_session(object(), 'grant', MagicMock())
            sample.assert_called_once()
            clean.assert_called_once()
