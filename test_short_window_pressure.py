"""Regression tests for sampled short-window taker-side pressure."""
import unittest
from market_intelligence import short_window_pressure, summarize_trades


class ShortWindowPressureTests(unittest.TestCase):
    def test_coverage_and_side(self):
        now=1000.0
        rows=[(991.0,100.0,1.0,'buy'),(999.0,100.0,2.0,'sell')]
        result=short_window_pressure(rows,now,10)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(result['imbalance'],-0.3333)
        self.assertEqual(result['trade_count'],2)

    def test_recent_only_is_partial(self):
        result=short_window_pressure([(999.0,100.0,1.0,'buy')],1000.0,30)
        self.assertEqual(result['status'],'partial')
        self.assertIn('insufficient',result['coverage'])

    def test_empty_is_partial_not_zero_confidence(self):
        result=short_window_pressure([],1000.0,10)
        self.assertEqual(result['status'],'partial')
        self.assertIsNone(result['imbalance'])

    def test_duplicate_trade_ids_not_double_counted(self):
        rows=[(991,100,1,'buy','same'),(992,100,1,'buy','same'),(999,100,1,'sell','other')]
        result=summarize_trades(rows,'sample',1000)
        self.assertEqual(result['trade_count'],2)
        self.assertIn('10s',result['short_windows'])
        self.assertIn('30s',result['short_windows'])


if __name__=='__main__':
    unittest.main()
