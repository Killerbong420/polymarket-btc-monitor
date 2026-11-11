"""Regression gates for the replacement of the retired 10-second probability meter."""
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
HTML=(ROOT/"terminal.html").read_text(encoding="utf-8")
APP=(ROOT/"app.py").read_text(encoding="utf-8")


class FormationVisualGuideTests(unittest.TestCase):
    def test_probability_meter_is_removed_from_default_page(self):
        for obsolete in ('id="directionMeter"','id="meterDownPct"',
                         'id="meterUpPct"','id="meterDownFill"',
                         'id="meterUpFill"','const PREDICTION_DISPLAY_MS=10000;',
                         'setInterval(updateMeter','fetch(\'/api/direction-probability\'',
                         '15-MIN BTC · 10s LIVE PREDICTION / HISTORICAL PROBABILITY'):
            self.assertNotIn(obsolete,HTML)

    def test_visual_guide_occupies_former_meter_slot(self):
        self.assertIn('id="formationVisualGuide"',HTML)
        self.assertIn('id="formationGuideChart"',HTML)
        self.assertIn('id="formationGuideTitle"',HTML)
        self.assertIn('id="formationGuideCall"',HTML)
        self.assertIn('id="formationGuideTrigger"',HTML)
        self.assertIn('id="formationGuideInvalidation"',HTML)
        self.assertIn('id="formationGuideObjective"',HTML)
        self.assertIn('id="formationReferenceGuide" open',HTML)
        self.assertIn("for(const id of ['formationVisualGuide','fastPanel','strongPanel','final15Panel'])",HTML)
        self.assertIn("chart.insertAdjacentElement('afterend',cards)",HTML)

    def test_all_six_reference_shapes_render_without_network_access(self):
        for kind in ("DOUBLE_TOP","DOUBLE_BOTTOM","BULL_FLAG","BEAR_FLAG","DOJI","INVALIDATED"):
            self.assertIn('data-guide="'+kind+'"',HTML)
        script=HTML.split('/* Read-only reference graphics.',1)[1].split('</script>',1)[0]
        self.assertIn("document.createElementNS(ns,tag)",script)
        self.assertIn("for(const ref of document.querySelectorAll('.formation-reference-card'))",script)
        self.assertIn("shapes={",script)
        self.assertNotIn("fetch(",script)
        self.assertNotIn("XMLHttpRequest",script)
        self.assertNotIn("WebSocket(",script)
        self.assertNotIn("setInterval(",script)

    def test_live_guide_tracks_only_fresh_coinbase_formation(self):
        script=HTML.split('/* Read-only reference graphics.',1)[1].split('</script>',1)[0]
        self.assertIn("window.btcServerStatus",script)
        self.assertIn("s.external?.early_patterns",script)
        self.assertIn("p.source==='Coinbase Exchange BTC-USD'",script)
        self.assertIn("p.candle_age_seconds<=75",script)
        self.assertIn("live&&p.status==='watch'",script)
        self.assertIn("tracked?.objective_usd",script)
        self.assertIn("kind='DOJI'",script)
        self.assertIn("phase==='INVALIDATED'",script)
        self.assertIn("phase==='EXPIRED'",script)
        self.assertIn("call='⊘ NO ACTIVE CALL'",script)
        self.assertIn("s.doji_watch",script)
        self.assertIn("btc-status-updated",script)
        self.assertIn("No Kraken substitution",script)
        self.assertNotIn("Kraken BTC/USD",script)
        self.assertNotIn("random(",script.lower())
        self.assertNotIn("Math.random(",script)
        self.assertIn("SCHEMATIC REFERENCE · NOT LIVE PRICE HISTORY",HTML)
        self.assertIn("not official Polymarket settlement prices",script)

    def test_underlying_brtI_predictions_unchanged_and_no_new_polling(self):
        self.assertIn("POLL = 5",APP)
        self.assertIn("guarded_live=guarded_decision(",APP)
        self.assertIn("state.update(reference_price=ref_public",APP)
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},4000)",HTML)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshTip()},3000)",HTML)
        self.assertIn("renderPatterns()",HTML)


if __name__=="__main__":
    unittest.main()
