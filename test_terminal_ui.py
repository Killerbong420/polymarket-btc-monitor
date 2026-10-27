"""Regression tests for the default BTC prediction dashboard.
Runs in GitHub Actions with the existing Python tests. Node checks inline browser JS syntax.
"""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest

HTML = Path(__file__).with_name("terminal.html")


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.in_script = False
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and not any(key == "src" for key, _ in attrs):
            self.in_script = True
            self.current = []

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.current))
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.current.append(data)


class TerminalUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_style_blocks_balanced(self):
        self.assertEqual(self.html.count("<style>"), self.html.count("</style>"))

    def test_all_four_panels_are_moved_to_default_view(self):
        for panel in ("fastPanel", "strongPanel", "final15Panel"):
            self.assertIn("document.getElementById(id);if(panel)cards.append(panel)", self.html)
            self.assertIn('id="' + panel + '"', self.html)
        self.assertIn("primary.id='primaryPanel';cards.append(primary)", self.html)
        self.assertIn("cards.id='terminalQuickCards'", self.html)

    def test_ai_win_loss_panel_always_visible(self):
        # A previous CSS selector hid this panel until the advanced toggle was opened.
        self.assertNotIn("body:not(.show-advanced) #serverHistory", self.html)
        self.assertIn('id="serverHistory"', self.html)
        self.assertIn('id="winLossRatio"', self.html)
        self.assertIn('id="winRate"', self.html)
        self.assertIn("d.ai_performance", self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},5000)", self.html)

    def test_ai_scoreboard_explains_100_settled_calls(self):
        self.assertIn("LAST 100 SETTLED PREDICTIONS", self.html)
        self.assertIn("WINS · LAST 100", self.html)
        self.assertIn("LOSSES · LAST 100", self.html)
        self.assertIn("PENDING · ALL", self.html)
        self.assertIn("performance.window_size||100", self.html)
        self.assertIn("most recent settled", self.html.lower())

    def test_obsolete_quote_card_completely_removed(self):
        self.assertNotIn("Market prices / order book", self.html)
        self.assertNotIn('id="marketStatus"', self.html)
        self.assertNotIn('id="officialQuote"', self.html)
        self.assertNotIn('$("marketStatus")', self.html)
        self.assertNotIn('$("officialQuote")', self.html)
        # Keep working market/quote inputs for the predictor, not the old UI.
        self.assertIn('id="futureForecastCard"', self.html)
        self.assertIn('id="futureForecastPrice"', self.html)

    def test_live_feeds_are_started(self):
        self.assertIn("connectStream('coinbase');connectStream('kraken');update()", self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},5000)", self.html)
        self.assertIn("renderPersistentSignals(d)", self.html)
        self.assertIn("window.btcPrimaryLocalLive=!!ok", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser JavaScript syntax checking")
    def test_inline_javascript_syntax(self):
        parser = InlineScripts()
        parser.feed(self.html)
        self.assertGreaterEqual(len(parser.scripts), 4)
        for i, script in enumerate(parser.scripts):
            result = subprocess.run(
                ["node", "--check"], input=script, text=True, capture_output=True, timeout=15
            )
            self.assertEqual(result.returncode, 0, "Inline script %s: %s" % (i, result.stderr))


if __name__ == "__main__":
    unittest.main()
