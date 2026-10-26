import unittest,json
from unittest.mock import patch
import app
class FeedbackTests(unittest.TestCase):
 def test_only_settled_other_markets_are_used(self):
  calls=json.dumps({'AI':{'direction':'UP','source':'AI_REVIEW','confidence':'HIGH'}})
  rows=[('old','EARLY','now','{}',calls,'DOWN'),('current','EARLY','now','{}',calls,'UP'),('pending','EARLY','now','{}',calls,None)]
  app._outcome_feedback_cache.clear()
  with patch('app.research_rows',return_value=rows):
   f=app.outcome_feedback('current')
  ai=[r for r in f['strategies'] if r['strategy']=='AI'][0]
  self.assertEqual(ai['wins'],0);self.assertEqual(ai['losses'],1)
