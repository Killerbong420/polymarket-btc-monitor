# Polymarket BTC Monitor


## Forecast the FUTURE 15-minute settlement number, not the current winning side

The live panel now forecasts the **future final 60-second BRTI mean** in USD alongside
the separate official price-to-beat. It is not the current BRTI tick and it
does not substitute Coinbase/Kraken spot or Polymarket contract odds.
`settlement_forecast.py` uses only already-published one-second BRTI ticks,
multi-horizon trend, momentum acceleration, realized reference volatility
and a deliberately small adjustment from fresh exchange candles, sampled
initiated-buy/sell pressure and Kraken order-book imbalance. The estimated
movement decays with forecast horizon; there are downside/upside *scenarios*
but no made-up confidence probabilities. Once the final minute begins, actual
published BRTI seconds enter the predicted final mean, and unpublished
seconds are forecast. Missing past ticks remain explicitly marked.

Both live and checkpoint OpenAI calls see the quantified future baseline
along with independent candles, chart patterns, time remaining, historical
research, exchange trade pressure, book depth and news. Their strict outputs
include the AI's **own numeric final BRTI estimate**, and the displayed/graded
UP/DOWN decision derives from whether **that estimated FUTURE number**
will be above or below the official price-to-beat. A current UP position
can therefore legitimately produce a predicted DOWN settlement, or vice
versa. If the BRTI feed is missing, no numeric BRTI price is manufactured.

The forward checkpoint log also saves an independent `PRICE_PATH` candidate
and the complete frozen forecast inputs without changing past results.
`GET /api/price-forecast/accuracy` reports absolute forecast errors in USD
for the AI, the projected path, and a naive unchanged-current-BRTI baseline,
**only** if all 60 actual final-minute BRTI seconds are published when the
market is officially settled. The default page shows the forecast and
observed errors when available. A lower error or higher win rate is not
guaranteed; measure improvement against unseen future rounds, and compare
models on matched rounds rather than small selective subsets.

Validation: `python -m unittest -v test_settlement_forecast`.


BTC 15-minute prediction monitoring project.


## Optional AI confirmation (server-side only)

The Safari terminal tracks only verified Polymarket US 15-minute contracts. Live AI reviews and the locked halfway prediction use that same market. No orders are placed.

1. Create an OpenAI API key at https://platform.openai.com/api-keys and enable API billing.
2. In Render, open the web service → Environment → Add Environment Variable.
3. Set `OPENAI_API_KEY` to the secret key. **Never** paste it into GitHub, Safari, chat messages, source code, or a public URL.
4. Optional: `OPENAI_MODEL=gpt-4o-mini` and `AI_INTERVAL_SECONDS=120` (minimum 60 seconds).
5. Save/redeploy. The AI CONFIRMATION panel and `/api/status` → `ai_confirmation` show activation status.

Without a key, the panel safely says NOT CONFIGURED and no OpenAI requests are made. Responses use a strict JSON schema and `store:false`. Errors do not disclose API keys. Calls cost money under the OpenAI API account separately from ChatGPT subscriptions. AI analysis receives current BTC chart evidence, the official target when published, and versioned historical research context. One final decision is recorded at 7:30 remaining. WIN/LOSS uses official settlement; historical frequencies are not calibrated live probabilities.

Render free instances may sleep, and their local SQLite files are ephemeral.

## Outside context

Both live AI reviews and locked 15-minute decisions receive sampled Coinbase/Kraken initiating buy/sell trade notional, Kraken top-20 book imbalance, and selected CoinDesk/Cointelegraph/Federal Reserve RSS headlines from the last 24 hours. Exchange samples refresh about every 15 seconds; news refreshes about every 120 seconds. Source failures and staleness are explicit. This is partial public coverage, not all trades, a verified on-chain flow feed, or guaranteed breaking-news detection. Locked decision evidence retains the context seen at call time.

## Forward research dashboard

New calls are captured at 12:00, 7:30, and 3:00 remaining in each verified 15-minute round (up to 60 seconds late). A missed checkpoint is labeled, never retroactively predicted. Each saved snapshot compares actual AI review, selective indicators, forced indicator direction and the observed target side. AI failures are excluded from AI accuracy and paired comparisons. Official settlement grades all saved checkpoints.

`/api/research` provides checkpoint/strategy performance, AI confidence labels with descriptive 95% Wilson intervals, paired AI-versus-baseline comparisons, and recent saved decisions. The report covers up to the latest 10,000 checkpoints. Earlier genuine halfway AI confidence is shown separately. `/api/research.csv` exports those calls and their evidence. Confidence labels are not calibrated probabilities; nearby rounds and checkpoints are dependent observations.

The public Polymarket US `crypto:btc` reference-price history endpoint supplies BRTI-aligned one-second observations when the contract explicitly identifies BRTI. Gap-filled buckets are excluded. Target behavior includes lead size, observed duration on each side, contiguous crossings, lead growth and coverage; spot fallback is labeled. Opening target reconstruction and the partial final-minute mean follow the official `[boundary-60s, boundary)` averaging rule. Published target and official outcomes remain authoritative.

`/api/entry-estimate` is a read-only single-fill, hold-to-settlement calculator using current US contract bid/ask sides, adjustable quantity/slippage, and standard taker coefficient 0.0695 verified October 25, 2026. Fees round to cents using half-even. No execution, liquidity guarantee or rebates are assumed. Recorded paper P&L defaults to 100 contracts and 0.5 cents adverse slippage, and only prices calls with an available entry-side quote. It is not actual trading performance or an expected-profit forecast. Check https://docs.polymarket.us/fees for changes.

News is labeled by publication age, heuristic commentary/report category, and possible old-URL republication. Similar headlines are collapsed. Event time and factual accuracy are not established by a headline; the AI's selected news IDs record stated influence, not proven causality. Both AI paths receive target behavior, reference data, quote and outside context.

`/api/health` shows stale-loop/feed/AI checks, missing targets, reference-target mismatch, persistence, missed-checkpoint and delayed-settlement alerts. Active alerts and state changes are saved in the existing database and shown on the page. No email, text, push or trade is sent. Configure persistent `DATABASE_URL` to retain all logs across deploys.

Run checks with `python -m unittest -v test_research`.

## Fake-money automated test bed

The shared paper sandbox starts with $1,000 fake cash. It never authenticates to Polymarket, imports trading credentials, or posts to an exchange: its only exchange request is public GET order-book data. Real orders cannot be enabled through this code.

It considers fresh LOW/MEDIUM/HIGH live AI short-term profit opportunities throughout each 15-minute round. The AI assesses entry costs and an exit target separately from its settlement-direction forecast. Each prediction is processed once; skipped entries can be reconsidered on a new prediction, with at most one filled entry per market. Calls over 60 seconds old, missing reference/target evidence, closed/stale books, one-sided books, wide spreads and insufficient eligible depth are skipped with reasons. Simulated whole-contract fills walk actual book levels, add 0.5 cents adverse slippage, charge the US standard taker fee, and obey $10/trade, $50 total open cost and $25/day loss limits. Quoted entry odds and all-in cost per contract must both be strictly below 85 cents, even for previously saved risk settings. Entries are rejected if current exit costs already breach the stop or the AI target cannot cover net profit after costs. Daily risk is gross settled losses on the current America/Chicago calendar day plus all remaining open cost; wins do not replenish this loss budget.

Cash is debited and positions recorded in the same database transaction with account locking and unique market constraints. Protective simulated sells target +5% net profit, stop at -8% net loss, and force exit after at most 180 seconds or 180 seconds before expiry, whichever comes first. These are full-position depth fills (fill-or-kill), not fictitious partial sales. Stop and time exits retry if liquidity is missing; an unfilled exit is shown explicitly. Only residual positions that could not exit use official settlement as an exceptional fallback, paying winning contracts once at $1/contract. The dashboard tracks cash, entry fees, realized P&L, conservative liquidation marks using exit-side depth and fees, and maximum observed drawdown. Missing/stale exit liquidity is valued at zero pending official settlement, so marks can be conservative. An observed paper profit is not proof of real-world profitability.

`GET /api/paper` exposes the read-only ledger, equity history and recent attempts. Pause/resume and emergency stop-and-exit controls affect only the shared fake-money sandbox; they require same-origin browser requests, but are not a private authentication system. Any dashboard visitor can pause/resume paper entries. Risk settings and starting cash are not editable through these endpoints. `PAPER_TRADING_ENABLED=false` in the server environment overrides resume. Pausing never prevents settlement. Pausing leaves protective sells active. Emergency stop pauses entries and schedules immediate simulated exit attempts. No exit fill is guaranteed if the feed or book is unavailable. No historical calls are backfilled, no paper balance is reset on deploy, and persistent DATABASE_URL retains the ledger.

The paper engine checks entries and protective sells on its own approximately five-second loop, independent of AI request latency. Live AI refresh defaults to 60 seconds (subject to AI_INTERVAL_SECONDS). No halfway entry trigger is required. An AI request to take an existing profit may sell below the fixed 10% target, but only when the fresh executable full-position exit is actually positive net of costs; stop/time exits remain active. No new entries are accepted within 30 seconds of the mandatory pre-expiry exit boundary. Old fixed checkpoints remain comparison research only.


The more active paper strategy requires a projected +5% net profit after fees and slippage (previously +10%), allowing smaller opportunities and earlier profit-taking. Existing accounts use the new threshold for new entries without resetting cash or history; already-open positions retain their recorded protective orders. The $10 trade cap, $25 daily risk budget, explicit AI confidence, strict 85-cent ceiling and expiry exits still apply.

More aggressive buys: explicit LOW-confidence live AI ENTER_UP/ENTER_DOWN opportunities can now enter with at most $5 fake-money cost, including fees. MEDIUM/HIGH entries retain the $10 cap. WAIT, missing/invalid confidence, stale signals, insufficient liquidity, fee-unprofitable targets and the existing loss/expiry limits still block entries. Existing saved accounts receive this policy without a balance reset. More entries may also mean more losses; profitability is not established.

## Missed-profit learning (paper only)

The server stores prospective, 15-minute Polymarket US quote scenarios in the persistent
paper ledger. A candidate is saved **before** a future quote is observed. Each scenario
uses 10 hypothetical contracts and fee/slippage estimates. The first subsequent event
to occur is classified as a +5% take-profit target, an -8% stop, or a timeout.
Missing future quotes become `UNOBSERVED`, not invented losses or wins.

`GET /api/paper` includes `missed_profit_learning` with segment summaries
(UP/DOWN and LOW/MID/HIGH entry odds), recent hypothetical outcomes, and
`missed_actual_exits`. Those separate exit-regret records compare the best
*previously observed full-depth simulated exit* on a real paper position with
its eventual sale or settlement. All dollar amounts are **simulated**.

The bounded learning rule starts disabled for each direction/odds segment until it
has at least 40 separate 15-minute markets with complete observations.
It requires a >=68% hypothetical take-profit hit rate, >=1.5% mean modelled
net return, and >=60% hit rates in both the older and more recent halves of
the sample. Only then may it slightly relax the paper-only Polymarket dip
entry filters. It never lifts the 85% all-in cap or other hard risk limits.
Disable automatic paper-policy adjustments with `PAPER_LEARNING_ENABLED=false`;
data collection and reporting continue.

Quote-level hypothetical outcomes are **not proof of available order-book depth,
a profitable real strategy, or a trained predictive AI model**. The learning
system cannot guarantee better results and does not place live orders.

Validation: `python -m unittest discover -p 'test_*.py' -v`.
