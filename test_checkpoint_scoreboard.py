import json
import unittest
from checkpoint_scoreboard import summarize_checkpoint_rows,build_scoreboard

class CheckpointBoardTest(unittest.TestCase):
    def test_only_frozen_official_price_path_calls_are_scored(self):
        def rec(market,ck,ai,path,winner,source='AI_REVIEW'):
            return (market,ck,json.dumps({'AI':{'direction':ai,'source':source},
                                           'PRICE_PATH':{'direction':path}}),winner)
        rows=[rec('m3','HALFWAY','UP','DOWN','UP'),
              rec('m2','HALFWAY','DOWN','DOWN','DOWN'),
              rec('m1','HALFWAY','UP','UP','DOWN'),
              rec('m1','HALFWAY','UP','UP','DOWN'), # duplicate excluded
              rec('m4','HALFWAY','DOWN','DOWN',None), # pending
              rec('m5','HALFWAY','DOWN','UP','UP',source='FALLBACK'),
              rec('m6','EARLY','WAIT','UP','UP')]
        board=summarize_checkpoint_rows(rows)
        self.assertEqual(len(board),3)
        self.assertTrue(all(x['method']=='PRICE_PATH' for x in board))
        path=next(x for x in board if x['checkpoint']=='HALFWAY')
        self.assertEqual((path['wins'],path['losses'],path['graded']),(2,2,4))
        self.assertEqual(path['accuracy_pct'],50.0)
        early=next(x for x in board if x['checkpoint']=='EARLY')
        self.assertEqual((early['wins'],early['graded']),(1,1))
        self.assertEqual([x['label'] for x in board],
                         ['12:00 left','7:30 left','3:00 left'])

    def test_only_15m_market_checkpoints_are_returned(self):
        result=build_scoreboard([])
        self.assertEqual(result["status"],"available")
        self.assertEqual(len(result["fifteen"]),3)
        self.assertNotIn("five", result)
        self.assertNotIn("late_audit", result)
        self.assertTrue(all(row["method"]=="PRICE_PATH" for row in result["fifteen"]))


if __name__=='__main__':unittest.main()
