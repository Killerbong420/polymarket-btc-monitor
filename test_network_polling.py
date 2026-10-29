"""Dashboard polling schedule regressions: low-latency live updates stay independent."""
import unittest
from pathlib import Path

HTML = (Path(__file__).parent / "terminal.html").read_text(encoding="utf-8")


class NetworkPollingTests(unittest.TestCase):
    def test_live_two_seconds_and_history_twenty_seconds(self):
        self.assertIn("fetch('/api/live',{cache:'no-store',signal:ctrl.signal})", HTML)
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},2000)", HTML)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshHistoryStatus()},20000)", HTML)
        self.assertIn("fetch('/api/status',{cache:'no-store',signal:ctrl.signal})", HTML)
        self.assertIn("const d={...cachedHistoryStatus,...live}", HTML)
        self.assertIn("history:full.history||[]", HTML)
        self.assertIn("ai_performance:full.ai_performance||{}", HTML)

    def test_one_live_fetch_with_reused_ai_events(self):
        # No extra API poll solely to update AI and news panels.
        self.assertEqual(HTML.count("fetch('/api/live'"), 1)
        self.assertIn("window.addEventListener('btc-status-updated',pollAI)", HTML)
        self.assertNotIn("setInterval(()=>{if(!document.hidden)pollAI()},20000)", HTML)

    def test_chart_tip_one_second_and_no_overlap(self):
        self.assertIn("if(document.hidden||tipBusy||typeof fetch!=='function')return", HTML)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshTip()},1000)", HTML)
        self.assertIn("setTimeout(()=>ctrl.abort(),3000)", HTML)
        self.assertIn("if(inFlight||Date.now()-historyReceivedAt<15000)return", HTML)
        self.assertIn("window.btcNoteRequest?.('chart history'", HTML)

    def test_browser_latency_is_collected_without_another_endpoint(self):
        self.assertIn("window.btcNetworkTimings=Object.create(null)", HTML)
        self.assertIn('id="networkTiming"', HTML)
        self.assertIn("window.btcNoteRequest?.('live'", HTML)
        self.assertIn("window.btcNoteRequest?.('BRTI tip'", HTML)


if __name__ == "__main__":
    unittest.main()
