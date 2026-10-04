[← back to main README](../README.md)

# EcoLogits

LiteLLM custom callback that tracks the carbon footprint and energy consumption of each LLM request and stores the results in PostgreSQL.

## Package Contents

```
ecologits/
├── ecologits_callback.py  # LiteLLM custom callback + impact HTTP server
├── requirements.txt       # Python dependencies (reference; installed via litellm/Dockerfile)
├── create_table.sql       # Table schema (reference/DR only — the callback creates it itself, see below)
└── README.md              # This file
```

## What This Does

```
LLM request completed (via LiteLLM)
         |
EcoLogitsCallback intercepts the response
         |
Computes carbon impact (gCO2eq) and energy (kWh)
using model parameters from the EcoLogits library
         |
Stores result in PostgreSQL (EcoLogits_Impacts table)
         |
Queryable via HTTP on port 4001
```

## How It Works

- Hooks into LiteLLM as a `CustomLogger` via `async_log_success_event`
- Retrieves model architecture (active/total parameters) from the EcoLogits model repository
- Falls back to 7B parameters if the model is not found
- Extracts the user email from the `x-openwebui-user-email` header and the chat ID from `x-openwebui-chat-id` (both forwarded by OpenWebUI)
- Exposes `GET /impacts/latest?user=<email>&chat_id=<id>` on port 4001 to query the latest impact entry plus the cumulated session total

## Database

`ecologits_callback.py` creates its own table (`CREATE TABLE IF NOT EXISTS`, wrapped in a Postgres
advisory lock to stay safe with LiteLLM's multiple gunicorn workers) the first time it's imported —
no manual step needed, on a fresh Postgres volume or an already-initialized one. `create_table.sql`
in this folder is kept only as a reference schema for manual ops/DR (`psql` direct), not wired into
`docker-entrypoint-initdb.d` (that only fires on first Postgres init, already past on any deployed
instance).

```sql
CREATE TABLE IF NOT EXISTS "EcoLogits_Impacts" (
    id SERIAL PRIMARY KEY,
    user_email TEXT,
    chat_id TEXT,
    model TEXT,
    tokens INTEGER,
    gwp_g FLOAT,
    energy_kwh FLOAT,
    timestamp TIMESTAMP DEFAULT NOW()
);
```

| Column      | Type      | Description                  |
|-------------|-----------|------------------------------|
| user_email  | text      | User email from OpenWebUI    |
| chat_id     | text      | OpenWebUI conversation ID, used for session totals |
| model       | text      | Model name (e.g. claude-3-5) |
| tokens      | int       | Output token count           |
| gwp_g       | float     | Carbon impact (gCO2eq)       |
| energy_kwh  | float     | Energy consumption (kWh)     |
| timestamp   | timestamp | Auto-set on insert           |

## Deployment

This callback is built into the main `litellm` image — see `../litellm/Dockerfile` at the repo root
(consolidated 13/09: installs `psycopg2-binary` + `ecologits[litellm]` for both optional callbacks in
one image, since a Compose service can only have one `build:`). `docker-compose.yml`'s `litellm`
service mounts this file as a volume:

```yaml
litellm:
  build:
    context: ./litellm
    dockerfile: Dockerfile
  ports:
    - "4000:4000"
    - "4001:4001"  # this callback
    - "4002:4002"  # cost-tracking
  volumes:
    - ./litellm-config.yaml:/app/config.yaml
    - ./ecologits/ecologits_callback.py:/app/ecologits_callback.py
    - ./cost-tracking/litellm_cost_callback.py:/app/litellm_cost_callback.py
```

Register the callback in `litellm-config.yaml` — under `callbacks:`, **not** `success_callback:`
(that key is reserved for Langfuse in this repo; `callbacks:` is the actual toggle for this one,
confirmed 13/09):

```yaml
litellm_settings:
  callbacks:
    - "ecologits_callback.proxy_handler_instance"
```

## Configuration

| Variable       | Description                        |
|----------------|------------------------------------|
| `DATABASE_URL` | PostgreSQL connection string (reuse LiteLLM's) |

## Supported Providers

Provider detection is automatic based on model name:
- `claude-*` → Anthropic
- `gpt-*`, `o1-*`, `o3-*`, `o4-*` → OpenAI
- `mistral-*`, `ministral-*` → MistralAI
- `gemini-*` → Google
- `provider/model` format → parsed directly

## Displaying the Impact in OpenWebUI Conversations

This callback only computes and stores the impact — it does not show anything to the end user by itself. To display the carbon footprint of each prompt directly below the assistant's response in the OpenWebUI chat, install the **`emissions_display.py`** Filter function from `../functions/`.

That Filter calls this callback's `GET /impacts/latest` endpoint after each response and appends the impact to the message — both the per-prompt impact and the cumulated session total (sum of `gwp_g`/`energy_kwh` for the conversation's `chat_id`), mirroring `cost-tracking/`'s `cost_display.py`. Without it installed and enabled in OpenWebUI (Admin Panel → Functions), the impact is tracked in the database but never surfaced in the chat UI.

## Notes

- Coexists with `cost-tracking/` (tested together 24/09, see the root README's "Optional Features" section) — different port (4001 vs 4002) and a different table, both already merged into the shared `litellm` image.

---

**Version:** 1.1
**Last Updated:** 2026-10-04
