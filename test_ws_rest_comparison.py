from ws_rest_comparison import compare_quotes

def test_match():
 ws={"market":"cpc-btc-updown-15m-x","bid":.34,"ask":.35,"received_at":100}
 rest={"status":"available","market":ws["market"],"bid":.34,"ask":.36,"fetched_epoch":100}
 assert compare_quotes(ws,rest,ws["market"],now=101)["status"]=="match"

def test_divergence():
 ws={"market":"x","bid":.1,"ask":.2,"received_at":100}
 rest={"status":"available","market":"x","bid":.4,"ask":.5,"fetched_epoch":100}
 assert compare_quotes(ws,rest,"x",now=101)["status"]=="divergent"

def test_stale_and_mismatch():
 ws={"market":"x","bid":.1,"ask":.2,"received_at":80}
 rest={"status":"available","market":"x","bid":.1,"ask":.2,"fetched_epoch":100}
 assert compare_quotes(ws,rest,"x",now=101)["reason"]=="ws_stale_or_missing"
 assert compare_quotes({**ws,"received_at":100},rest,"y",now=101)["status"]=="unavailable"
