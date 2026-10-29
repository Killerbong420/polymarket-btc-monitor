"""Regression checks for the official BRTI chart's visual-only 30 FPS scheduler."""
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent / "terminal.html").read_text(encoding="utf-8")


class ChartThirtyFpsTests(unittest.TestCase):
    def test_chart_caps_event_driven_draws_without_idle_animation_loop(self):
        self.assertIn("nowMillis-lastDraw<1000/30", HTML)
        self.assertNotIn("requestAnimationFrame(animate)", HTML)
        self.assertIn("setInterval(()=>{if(!document.hidden)draw()},1000)", HTML)
        self.assertIn("window.addEventListener('btc-status-updated',()=>draw())", HTML)
        self.assertIn("tipTicks=[...merged.entries()].sort((a,b)=>a[0]-b[0]).slice(-32);", HTML)
        self.assertNotIn("lastDraw=0;status.textContent", HTML)

    def test_chart_preserves_one_second_verified_price_poll(self):
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshTip()},1000)", HTML)
        self.assertIn("data.source!=='Polymarket US BTC reference / BRTI'", HTML)
        self.assertIn("if(!verified.length)return;", HTML)

    def test_verified_chart_tip_can_outlive_slow_dashboard_status(self):
        # The chart has its own verified one-second feed. A slow /api/status
        # heartbeat must not hide newer official BRTI observations.
        self.assertIn("chart_tip_verified:true", HTML)
        self.assertIn("!d.chart_tip_verified&&", HTML)
        self.assertIn("d?.reference_price?.source==='Polymarket US BTC reference / BRTI'", HTML)
        self.assertIn("d.reference_price?.source!==data.source", HTML)
        self.assertIn("data.market!==d.market", HTML)
        self.assertIn("if(!verified.length)return;", HTML)

    def test_canvas_dimensions_are_only_reset_when_changed(self):
        self.assertIn("if(canvas.width!==pixelWidth)canvas.width=pixelWidth;", HTML)
        self.assertIn("if(canvas.height!==pixelHeight)canvas.height=pixelHeight;", HTML)
