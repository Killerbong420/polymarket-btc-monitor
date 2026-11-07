import unittest
from probability_calibration import calibrate, bucket

class ProbabilityCalibrationTests(unittest.TestCase):
 def test_empty(self):
  self.assertEqual(calibrate([],0)["status"],"unavailable")
 def test_no_leakage_from_current_market(self):
  rows=[("market-"+str(i),0.1,"UP") for i in range(200)]
  self.assertEqual(calibrate(rows,0.1,current_market="market-199")["samples"],199)
 def test_bucket(self):
  self.assertIsNone(bucket(None))
  self.assertEqual(bucket(0),3)
 def test_no_fake_probability_from_all_wins(self):
  rows=[("market-"+str(i),0.1,"UP") for i in range(200)]
  self.assertLess(calibrate(rows,0.1).get("up_probability",1),1)

if __name__=="__main__":unittest.main()
