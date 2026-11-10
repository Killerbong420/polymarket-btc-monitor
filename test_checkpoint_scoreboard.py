import json
import unittest
from checkpoint_scoreboard import summarize_checkpoint_rows,build_scoreboard

class CheckpointBoardTest(unittest.TestCase):
    def test_only_verified_independently_frozen_calls_count(self):
        def rec(market,ck,ai,path,winner,source='AI_REVIEW'):
            return (market,ck,json.dumps({'AI':{'direction':ai,'source':source},'PRICE_PATH':{'direction':path}}),winner)
        rows=[rec('m3','HALFWAY','UP','DOWN','UP'),
              rec('m2','HALFWAY','DOWN','DOWN','DOWN'),
              rec('m1','HALFWAY','UP','UP','DOWN'),
              rec('m1','HALFWAY','UP','UP','DOWN'), # duplicate does not count
              rec('m4','HALFWAY','DOWN','DOWN',None), # pending excluded
              rec('m5','HALFWAY','DOWN','UP','UP',source='FALLBACK'), # not genuine AI
              rec('m6','EARLY','WAIT','UP','UP')]
        board=summarize_checkpoint_rows(rows)
        ai=next(x for x in board if x['checkpoint']=='HALFWAY' and x['method']=='AI')
        path=next(x for x in board if x['checkpoint']=='HALFWAY' and x['method']=='PRICE_PATH')
        self.assertEqual((ai['wins'],ai['losses'],ai['graded']),(2,1,3))
        self.assertEqual((path['wins'],path['losses'],path['graded']),(2,2,4))
        self.assertEqual(ai['excluded_no_call'],1)
        early_ai=next(x for x in board if x['checkpoint']=='EARLY' and x['method']=='AI')
        self.assertEqual(early_ai['graded'],0)
        self.assertIsNone(early_ai['accuracy_pct'])

    def test_only_15m_market_checkpoints_are_returned(self):
        result=build_scoreboard([])
        self.assertEqual(result["status"],"available")
        self.assertEqual(len(result["fifteen"]),6)
        self.assertNotIn("five", result)
        self.assertNotIn("late_audit", result)


if __name__=='__main__':unittest.main()
