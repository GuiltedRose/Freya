"""Saved candle compatibility and retired web transport guards."""
import copy
import unittest
from unittest.mock import Mock
from data_policy import SourceAccessBlocked
from pump_market import PumpMarketSources,pump_history

MINT='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
POOL='11111111111111111111111111111111'


class PumpMarketTests(unittest.TestCase):
    def setUp(self):
        self.candles={'served':{'res':'1m','measure':'price_usd'},'hasOlder':True,
                      'candles':[{'openTimeMs':(600+i*60)*1000,'open':'.57','high':'.58',
                                  'low':'.56','close':'.57','volumeUsd':'100','final':True} for i in range(6)]}

    def test_retired_collection_never_calls_even_an_injected_fetcher(self):
        fetch=Mock()
        with self.assertRaises(SourceAccessBlocked):PumpMarketSources(fetch=fetch).observation(MINT,1000)
        fetch.assert_not_called()

    def test_saved_candles_preserve_price_units_and_identity(self):
        row=pump_history(self.candles,MINT,POOL,'saved-fixture',1000)
        self.assertEqual((row['mint'],row['pool']),(MINT,POOL))
        self.assertEqual(row['measure'],'price_usd')
        self.assertEqual(row['interval_seconds'],60)
        self.assertEqual(row['candles'][0]['volume_usd'],100)

    def test_market_cap_and_five_minute_data_cannot_become_price_minutes(self):
        for served in ({'res':'5m','measure':'price_usd'},{'res':'1m','measure':'market_cap_usd'}):
            bad=copy.deepcopy(self.candles);bad['served']=served
            with self.assertRaises(ValueError):pump_history(bad,MINT,POOL,'saved-fixture',1000)

    def test_partial_candles_are_excluded_and_duplicates_rejected(self):
        self.candles['candles'][-1]['final']=False
        self.assertEqual(len(pump_history(self.candles,MINT,POOL,'saved-fixture',1000)['candles']),5)
        self.candles['candles'].append(copy.deepcopy(self.candles['candles'][0]))
        with self.assertRaises(ValueError):pump_history(self.candles,MINT,POOL,'saved-fixture',1000)
