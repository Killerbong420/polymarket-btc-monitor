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
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},2000)", self.html)

    def test_lightweight_brtI_line_without_candles(self):
        # Render the official index as a simple line; retain server-side
        # price-path analysis and avoid additional polling requests.
        for canvas_id in ("candleChart", "priceChart", "brtiCanvas"):
            self.assertNotIn('id="' + canvas_id + '"', self.html)
        for endpoint in ("/api/candles", "/api/brti-chart"):
            self.assertNotIn("fetch('" + endpoint + "'", self.html)
        self.assertIn('id="brtiLineCanvas"', self.html)
        self.assertIn('id="brtiLineStatus"', self.html)
        self.assertIn("window.btcBrtiLineModel=model", self.html)
        self.assertIn("window.addEventListener('btc-status-updated',()=>draw())", self.html)
        self.assertNotIn("ENTRY COST SCENARIO", self.html)
        self.assertNotIn('id="costCalculate"', self.html)
        self.assertNotIn('id="costDirection"', self.html)
        self.assertNotIn('id="costSlippage"', self.html)
        self.assertNotIn('id="costQuantity"', self.html)
        self.assertIn('id="pmApiStrip"', self.html)
        self.assertIn('id="patternName"', self.html)
        self.assertIn("ext.chart_pattern", self.html)
        self.assertIn("if(statusInFlight)return", self.html)
        self.assertIn("if(fiveBusy||document.hidden)return", self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshFive()},2000)", self.html)

    def test_mobile_layout_uses_compact_order_and_expands_advanced_evidence(self):
        self.assertIn('id="mainTitle"', self.html)
        self.assertIn('id="mainSubtitle"', self.html)
        self.assertIn("chart.insertAdjacentElement('beforebegin',title)", self.html)
        self.assertIn("if(forecast)cards.insertAdjacentElement('afterend',forecast)", self.html)
        self.assertIn("if(history)(exchanges||forecast||cards).insertAdjacentElement('afterend',history)", self.html)
        self.assertIn("if(flip){flip.id='flipPanel';panel.insertAdjacentElement('afterend',flip);}", self.html)
        self.assertIn("patternFold=document.createElement('details')", self.html)
        self.assertIn("disclosure.id='fivePathDetails'", self.html)
        self.assertIn("if(!el('fivePathDetails')?.open)", self.html)
        self.assertIn("const shell=document.createElement('details');shell.className='verified-rounds'", self.html)
        self.assertIn("body:not(.show-advanced) #marketDataPanel", self.html)
        self.assertIn("body:not(.show-advanced) #dataDiagnosticsPanel", self.html)
        self.assertNotIn("body:not(.show-advanced) .terminal-secondary,\\n\\n#advancedToggle", self.html)

    def test_verified_cross_exchange_consensus_and_checkpoints(self):
        self.assertIn('id="crossExchangePanel"', self.html)
        self.assertIn('id="crossMedian"', self.html)
        self.assertIn('id="crossSpread"', self.html)
        self.assertIn('id="crossBRTI"', self.html)
        self.assertIn('id="crossExchangeRows"', self.html)
        self.assertIn('id="crossPressure"', self.html)
        self.assertIn('id="checkpointBoard"', self.html)
        self.assertIn('id="checkpointRows"', self.html)
        self.assertIn("fetch('/api/checkpoint-scoreboard'", self.html)
        self.assertIn("window.addEventListener('btc-status-updated',renderExchange)", self.html)
        self.assertIn("if(exchanges)(forecast||cards).insertAdjacentElement('afterend',exchanges)", self.html)
        self.assertIn("if(checkpoint)(history||exchanges||forecast||cards).insertAdjacentElement('afterend',checkpoint)", self.html)
        self.assertIn("USDT", self.html)
        self.assertNotIn('id="costCalculate"', self.html)

    def test_trading_style_chart_preserves_official_brtI_and_stored_exchange_samples(self):
        self.assertIn('id="brtiRangeTabs"', self.html)
        for period in ('round','30s','1m','2m','5m','30m','60m'):
            self.assertIn('data-range="' + period + '"', self.html)
        self.assertIn('id="brtiLineCanvas"', self.html)
        self.assertIn('id="brtiOverlayLegend"', self.html)
        self.assertIn('window.btcBrtiLineModel=model', self.html)
        self.assertIn('window.btcChartSelectRange=onRange', self.html)
        self.assertIn('fetch(\'/api/market-chart/history?range=\'+encodeURIComponent(requested)', self.html)
        self.assertIn('SETTLEMENT AVG', self.html)
        self.assertIn('m.target!==null', self.html)
        self.assertIn("if(m.projected!==null)line([last,[m.end,m.projected]]", self.html)
        self.assertNotIn('id="candleChart"', self.html)
        self.assertNotIn("fetch('/api/candles'", self.html)
        self.assertIn("if(typeof setInterval==='function'){", self.html)

    def test_chart_one_second_ticks_and_fast_default_ui(self):
        self.assertIn("fetch('/api/market-chart/tip'",self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshTip()},1000)",self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)draw()},1000)",self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshHistory()},10000)",self.html)
        self.assertIn("if(!force&&nowMillis-lastDraw<950)return",self.html)
        self.assertIn("data.source!=='Polymarket US BTC reference / BRTI'",self.html)
        self.assertIn("tipMarket!==d.market",self.html)
        self.assertIn("tipBusy||typeof fetch!=='function'",self.html)
        self.assertIn("document.body.classList.contains('show-advanced')",self.html)
        self.assertIn("const legacyActive=()=>!document.hidden",self.html)

    def test_ai_scoreboard_explains_100_settled_calls(self):
        self.assertIn("LAST 100 SETTLED PREDICTIONS", self.html)
        self.assertIn("WINS · LAST 100", self.html)
        self.assertIn("LOSSES · LAST 100", self.html)
        self.assertIn("PENDING · ALL", self.html)
        self.assertIn("performance.window_size||100", self.html)
        self.assertIn("most recent settled", self.html.lower())

    def test_quick_prediction_shows_plain_up_down_and_first5_accuracy(self):
        self.assertIn("⚡ QUICK PREDICTION · UP / DOWN", self.html)
        self.assertNotIn("▲ FAST UP", self.html)
        self.assertNotIn("▼ FAST DOWN", self.html)
        self.assertNotIn("'LAST FAST '+fastDirection", self.html)
        self.assertIn("id=\"quickAccuracySummary\"", self.html)
        self.assertIn("id=\"quickAccuracyMinutes\"", self.html)
        self.assertIn("id=\"quickAccuracyRecent\"", self.html)
        self.assertIn("fetch('/api/quick-accuracy'", self.html)
        self.assertIn("refreshFirstFiveQuickAccuracy()", self.html)

    def test_obsolete_quote_card_completely_removed(self):
        self.assertNotIn("Market prices / order book", self.html)
        self.assertNotIn('id="marketStatus"', self.html)
        self.assertNotIn('id="officialQuote"', self.html)
        self.assertNotIn('$("marketStatus")', self.html)
        self.assertNotIn('$("officialQuote")', self.html)
        # Keep working market/quote inputs for the predictor, not the old UI.
        self.assertIn('id="futureForecastCard"', self.html)
        self.assertIn('id="futureForecastPrice"', self.html)

    def test_five_minute_path_focuses_on_brtI_without_market_value(self):
        panel = self.html.split("/* Secondary, independently graded five-minute market;",1)[1]
        self.assertIn('id="fivePathChart"', panel)
        self.assertIn('id="fiveLiveProjection"', panel)
        self.assertIn('id="fiveReversal"', panel)
        self.assertIn("official_target_usd", panel)
        self.assertIn("projected_settlement_average_usd", panel)
        self.assertNotIn("fair price", panel.lower())
        self.assertNotIn("auto-trader", panel.lower())
        self.assertNotIn("Chance of UP", panel)
        self.assertIn("No contract pricing or trade signals", panel)

    def test_last_36_verified_round_tiles_for_both_markets(self):
        self.assertIn("LAST 36 VERIFIED 15-MINUTE ROUNDS",self.html)
        self.assertIn("LAST 36 VERIFIED 5-MINUTE ROUNDS",self.html)
        self.assertIn("fiveLateTiles",self.html)
        self.assertIn("fetch('/api/round-tiles'",self.html)
        self.assertIn("correct call",self.html)
        self.assertIn("No verified",self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshRoundTiles()},10000)",self.html)
        tiles=self.html.split("/* Recent verified rounds.",1)[1]
        self.assertNotIn("fair price",tiles.lower())
        self.assertNotIn("auto-trader",tiles.lower())
        self.assertNotIn("market value",tiles.lower())

    def test_live_feeds_are_started(self):
        self.assertIn("if(legacyActive())startLegacy();else stopLegacy()", self.html)
        self.assertIn("for(const which of ['coinbase','kraken'])", self.html)
        self.assertIn("if(legacyActive())update()", self.html)
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},2000)", self.html)
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
