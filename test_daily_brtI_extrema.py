"""Regression checks for the observed UTC-day BRTI extrema display."""
import unittest
from unittest.mock import patch
import market_chart_history as chart
from pathlib import Path

class DailyObservedExtremaTest(unittest.TestCase):
 def setUp(self):
  chart._brti.clear()
  chart._day_extrema.clear()

 def tearDown(self):
  chart._brti.clear()
  chart._day_extrema.clear()

 def test_high_low_from_official_ticks_only_and_no_lookahead(self):
  now=1794139200  # fixed UTC timestamp
  chart.record_brti([[now-20,100],[now-10,120],[now-5,90],[now+20,999]],chart.OFFICIAL_SOURCE,now)
  d=chart.today_observed_extrema(now)
  self.assertEqual((d['high'],d['low']),(120,90))
  self.assertEqual(d['coverage'],'since_server_observation')
  chart.record_brti([[now-1,999]],'Coinbase',now)
  self.assertEqual(chart.today_observed_extrema(now)['high'],120)

 def test_day_rollover_does_not_reuse_yesterday(self):
  now=1794139200
  chart.record_brti([[now-1,110]],chart.OFFICIAL_SOURCE,now)
  tomorrow=now+86400
  self.assertIsNone(chart.today_observed_extrema(tomorrow)['high'])
  chart.record_brti([[tomorrow-1,95]],chart.OFFICIAL_SOURCE,tomorrow)
  self.assertEqual(chart.today_observed_extrema(tomorrow)['low'],95)

 def test_display_discloses_partial_coverage(self):
  html=Path(__file__).with_name('terminal.html').read_text()
  for marker in ('id="brtiTodayHigh"','id="brtiTodayLow"','today_observed','earlier daily highs/lows may be missing'):
   self.assertIn(marker,html)

if __name__=='__main__':
 unittest.main()
