# Polymarket BTC Monitor

BTC 5-minute prediction monitoring project.


## Optional AI confirmation (server-side only)

The original Safari BTC terminal and 5/15-minute switch remain intact. AI confirmation is a separate read-only panel: it **does not** change FAST CALL, place orders, or claim calibrated win probabilities.

1. Create an OpenAI API key at https://platform.openai.com/api-keys and enable API billing.
2. In Render, open the web service → Environment → Add Environment Variable.
3. Set `OPENAI_API_KEY` to the secret key. **Never** paste it into GitHub, Safari, chat messages, source code, or a public URL.
4. Optional: `OPENAI_MODEL=gpt-4o-mini` and `AI_INTERVAL_SECONDS=120` (minimum 60 seconds).
5. Save/redeploy. The AI CONFIRMATION panel and `/api/status` → `ai_confirmation` show activation status.

Without a key, the panel safely says NOT CONFIGURED and no OpenAI requests are made. Responses use a strict JSON schema and `store:false`. Errors do not disclose API keys. Calls cost money under the OpenAI API account separately from ChatGPT subscriptions. AI analysis is based only on the available server snapshot (currently the 5-minute market); it is **not** independent 15-minute contract analysis or official settlement verification.

Render free instances may sleep, and their local SQLite files are ephemeral.
