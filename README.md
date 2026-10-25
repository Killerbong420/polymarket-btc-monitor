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
