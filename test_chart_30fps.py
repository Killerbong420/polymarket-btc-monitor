"""Regression checks for the official BRTI chart's visual-only 30 FPS scheduler."""
from pathlib import Path

HTML = (Path(__file__).resolve().parent / "terminal.html").read_text(encoding="utf-8")


def test_chart_uses_30fps_cap_and_animation_frames():
    assert "nowMillis-lastDraw<1000/30" in HTML
    assert "requestAnimationFrame(animate)" in HTML
    assert "if(!document.hidden)draw();" in HTML


def test_chart_preserves_one_second_verified_price_poll():
    assert "setInterval(()=>{if(!document.hidden)refreshTip()},1000)" in HTML
    assert "data.source!=='Polymarket US BTC reference / BRTI'" in HTML
    assert "if(!verified.length)return;" in HTML


def test_canvas_dimensions_are_only_reset_when_changed():
    assert "if(canvas.width!==pixelWidth)canvas.width=pixelWidth;" in HTML
    assert "if(canvas.height!==pixelHeight)canvas.height=pixelHeight;" in HTML
