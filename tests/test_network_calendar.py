import tempfile
import unittest
from pathlib import Path
from solana_network_budget import Budget, calendar_month, timestamp


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.budget = Budget(Path(self.temp.name)/'budget.sqlite')
        self.now = timestamp('2026-11-03T12:00:00Z')

    def test_month_renewal_preserves_grants_and_drops_reserve_authorization(self):
        self.budget.initialize_calendar(self.now)
        grant = self.budget.grant(1000,self.now)
        self.budget.authorize_reserve(self.now)
        december = timestamp('2026-12-01T00:00:00Z')
        self.assertEqual(self.budget.report(december-1)['budget_debited_bytes'],1000)
        report = self.budget.report(december)
        self.assertEqual(report['budget_debited_bytes'],0)
        self.assertFalse(report['reserve_authorized'])
        with self.budget.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM grants').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM cycle_history').fetchone()[0],1)
        self.budget.sample(grant['id'],1100,100,december)
        self.assertEqual(self.budget.report(december)['budget_debited_bytes'],0)
        with self.assertRaises(ValueError): self.budget.report(self.now)

    def test_adoption_never_refunds_current_usage(self):
        self.budget.initialize('2026-10-15T00:00:00Z','2026-11-15T00:00:00Z',self.now)
        self.budget.grant(1000,self.now)
        self.budget.enable_calendar(self.now)
        self.assertEqual(self.budget.report(self.now)['budget_debited_bytes'],1000)
        self.assertEqual(self.budget.report(self.now)['cycle_end_utc'],'2026-12-01T00:00:00+00:00')
        self.budget.enable_calendar(self.now)
        self.assertEqual(self.budget.report(self.now)['budget_debited_bytes'],1000)

    def test_december_and_leap_february_boundaries(self):
        for now,end in [('2026-12-15T00:00:00Z','2027-01-01T00:00:00Z'),
                        ('2028-02-15T00:00:00Z','2028-03-01T00:00:00Z')]:
            self.assertEqual(calendar_month(timestamp(now))[1],timestamp(end))
