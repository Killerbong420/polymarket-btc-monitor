# Polymarket BTC Monitor

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
