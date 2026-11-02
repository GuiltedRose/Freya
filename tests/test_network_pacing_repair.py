"""Offline envelope checks; kernel behavior requires the NixOS lifecycle VM."""
import unittest
from solana_network_budget import firewall,shaping_commands


class NetworkPacingRepairTests(unittest.TestCase):
    def test_policing_envelope_and_quota_remain_bounded(self):
        rules=firewall({'id':'a'*32,'bytes':1000,'cycle_end':1060},now=1000)
        self.assertEqual(rules.count('limit rate over 16 kbytes/second burst 16 kbytes counter name'),2)
        self.assertIn('counter name download_rate_drop drop',rules)
        self.assertIn('counter name upload_rate_drop drop',rules)
        self.assertIn('until 1000 bytes',rules)
        self.assertIn('timeout 60s',rules)
        self.assertEqual(rules.count('policy drop'),3)
        self.assertLess(rules.index('counter name download'),rules.index('limit rate over'))

    def test_shaper_does_not_emit_sixteen_kilobyte_microbursts(self):
        commands=shaping_commands(upload_kbytes=16,download_kbytes=16)
        self.assertEqual(len(commands),2)
        for command in commands:
            self.assertIn('rate 128kbit burst 1500b latency 100ms',command)
            self.assertNotIn('burst 16kb',command)

    def test_lifecycle_upload_policing_follows_namespace_shaper(self):
        from solana_network_lifecycle import host_rules
        grant={'id':'a'*32,'bytes':1000,'cycle_end':1060}
        rules=firewall(grant,now=1000,upload_policed_on_host=True)
        self.assertNotIn('counter name upload_rate_drop drop',rules)
        self.assertIn('counter name download_rate_drop drop',rules)
        self.assertIn('quota name shared accept',rules)
        host=host_rules('wan0',upload_kbytes=8)
        self.assertIn('iifname "solana-host" oifname "wan0" limit rate over 8 kbytes/second burst 16 kbytes counter name upload_rate_drop drop',host)
