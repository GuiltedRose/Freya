"""Explicit extended leases retain finite kernel expiration and byte budgets."""
import unittest
from solana_network_budget import firewall


class LeaseTests(unittest.TestCase):
    def test_default_lease_remains_sixty_seconds(self):
        grant={'id':'a'*32,'bytes':1000,'cycle_end':10000}
        self.assertIn('timeout 60s',firewall(grant,now=100))

    def test_explicit_longer_lease_is_finite_and_cycle_bounded(self):
        grant={'id':'a'*32,'bytes':1000,'cycle_end':350}
        rendered=firewall(grant,now=100,session_seconds=300)
        self.assertIn('timeout 250s',rendered)
        self.assertIn('1000 bytes',rendered)
        for invalid in (0,3601,True):
            with self.assertRaises(ValueError):firewall(grant,now=100,session_seconds=invalid)
