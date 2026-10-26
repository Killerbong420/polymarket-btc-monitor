"""Independent public-indicator research strategies, not external bot execution.

Concept families similar to open strategy libraries (Hummingbot/Freqtrade):
MACD trend continuation, Bollinger mean reversion, and ATR/Supertrend.
All signals are fixed using completed 1-minute BTC exchange candles.
They are NOT calibrated contract settlement forecasts and never place orders.
"""
import math

STRATEGIES = ("BOT_MACD", "BOT_BOLLINGER", "BOT_SUPERTREND")
VERSION = "public-bot-study-v1"


def _waiting(reason):
    return {name: {"direction": "WAIT", "source": VERSION,
                   "confidence": "UNRATED", "reason": reason,
                   "model_version": VERSION} for name in STRATEGIES}


def _call(name, direction, reason, **fields):
    return {"direction": direction, "source": VERSION, "confidence": "UNRATED",
            "reason": reason, "model_version": VERSION, **fields}


def _ema(data, period):
    weight = 2.0 / (period + 1.0)
    value = data[0]
    result = []
    for price in data:
        value += weight * (price - value)
        result.append(value)
    return result


def study_strategies(candles, source="external-exchange-1m"):
    """Produce simultaneous forward-only independent forecasts, with abstentions."""
    try:
        bars = [[float(x[i]) for i in range(6)] for x in candles[-80:]]
    except (IndexError, TypeError, ValueError):
        return _waiting("Could not parse complete exchange OHLCV")
    if len(bars) < 55:
        return _waiting("55 contiguous 1m candles required")
    if any(not all(math.isfinite(v) for v in b) or min(b[1:5]) <= 0 or
           b[1] > min(b[3], b[4]) or b[2] < max(b[3], b[4]) or b[5] < 0
           for b in bars):
        return _waiting("OHLCV sanity checks failed")
    if any(round(b[0] - a[0]) != 60 for a, b in zip(bars, bars[1:])):
        return _waiting("Missing one-minute candles: no predictions")

    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[1] for b in bars]
    last = closes[-1]
    tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
              abs(lows[i]-closes[i-1])) for i in range(1, len(bars))]
    atr = sum(tr[-14:]) / 14
    if atr <= 0:
        return _waiting("Zero measurable BTC movement")

    # MACD histogram crossover+trend: do not force directions on a flat crossover.
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    macd = [a-b for a, b in zip(ema12, ema26)]
    signal = _ema(macd, 9)
    hist = [a-b for a, b in zip(macd, signal)]
    threshold = max(last * 0.000005, atr * 0.05)
    trend_up = ema12[-1] > ema26[-1] and hist[-1] > threshold and hist[-1] > hist[-2]
    trend_down = ema12[-1] < ema26[-1] and hist[-1] < -threshold and hist[-1] < hist[-2]
    macd_side = "UP" if trend_up else "DOWN" if trend_down else "WAIT"

    # Bollinger mean reversion: two consecutive candles back inside from outside
    # an established 20-bar, 2-sigma band. No directional call merely from touching a band.
    def band(values):
        avg = sum(values)/len(values)
        std = math.sqrt(sum((v-avg)**2 for v in values)/len(values))
        return (avg-2*std, avg, avg+2*std)
    lower_previous, mid_previous, upper_previous = band(closes[-22:-2])
    lower_now, mid_now, upper_now = band(closes[-20:])
    rsi_moves = [closes[i]-closes[i-1] for i in range(-14, 0)]
    gain = sum(max(0, z) for z in rsi_moves)
    loss = sum(max(0, -z) for z in rsi_moves)
    rsi = 100.0 if loss == 0 and gain else 50.0 if loss == 0 else 100-100/(1+gain/loss)
    lower_reclaim = closes[-3] < lower_previous and closes[-2] > lower_previous and closes[-1] > closes[-2] and rsi < 48
    upper_reject = closes[-3] > upper_previous and closes[-2] < upper_previous and closes[-1] < closes[-2] and rsi > 52
    bb_side = "UP" if lower_reclaim else "DOWN" if upper_reject else "WAIT"

    # Previous 20-bar channel plus ATR and two *completed* closes confirm a
    # Supertrend-like trend breakout. Independent, not a copied library implementation.
    upper = max(highs[-23:-3])
    lower = min(lows[-23:-3])
    pad = max(last*0.00002, atr*0.2)
    super_up = closes[-2] > upper+pad and closes[-1] > upper+pad
    super_down = closes[-2] < lower-pad and closes[-1] < lower-pad
    super_side = "UP" if super_up else "DOWN" if super_down else "WAIT"

    meta = {"candle_source": source, "horizon": "15m contract proxy; 1m completed candles",
            "atr_usd": round(atr, 3)}
    return {
        "BOT_MACD": _call("BOT_MACD", macd_side, "EMA12/26 and strengthening MACD histogram must align",
                          **meta, indicator="macd_histogram", histogram=round(hist[-1], 5)),
        "BOT_BOLLINGER": _call("BOT_BOLLINGER", bb_side, "Confirmed Bollinger-band re-entry, RSI and continuation",
                               **meta, indicator="bollinger_reentry", rsi14=round(rsi, 2)),
        "BOT_SUPERTREND": _call("BOT_SUPERTREND", super_side,
                                "Two completed closes outside prior 20m range with ATR buffer",
                                **meta, indicator="atr_channel", support=round(lower, 2),
                                resistance=round(upper, 2)),
    }


def consensus(calls):
    """Optional research-only multi-bot vote, no hindsight weighting.
    Requires AI plus at least two non-AI independent signals agreeing. Else WAIT.
    """
    ai = calls.get("AI") or {}
    if ai.get("source") != "AI_REVIEW" or ai.get("direction") not in ("UP", "DOWN"):
        return _call("BOT_CONSENSUS", "WAIT", "No genuine AI review")
    names = ("BOT_MACD", "BOT_BOLLINGER", "BOT_SUPERTREND", "PRICE_ACTION")
    votes = [(name, (calls.get(name) or {}).get("direction")) for name in names]
    good = [(name, v) for name, v in votes if v in ("UP", "DOWN")]
    if len(good) < 2:
        return _call("BOT_CONSENSUS", "WAIT", "Fewer than two independent research bot calls",
                     total_votes=len(good))
    up = sum(v == "UP" for _, v in good)
    down = len(good)-up
    chosen = ai["direction"]
    if (chosen == "UP" and up < 2) or (chosen == "DOWN" and down < 2) or min(up,down) > 0:
        return _call("BOT_CONSENSUS", "WAIT", "Bot votes disagree with AI or each other",
                     votes_up=up, votes_down=down)
    return _call("BOT_CONSENSUS", chosen,
                 "Genuine AI plus independent price-action bots agree; research only",
                 votes_up=up, votes_down=down)
