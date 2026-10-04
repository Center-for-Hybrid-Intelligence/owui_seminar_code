[← back to main README](../README.md)

# Cost Tracking

LiteLLM custom callback that tracks the dollar cost (input + output tokens combined) of each LLM request and stores the results in PostgreSQL. Same architecture as `ecologits/`, applied to cost instead of carbon.

## Package Contents

```
cost-tracking/
├── litellm_cost_callback.py  # LiteLLM custom callback + cost HTTP server
├── Dockerfile                 # Extends LiteLLM image with psycopg2-binary
├── requirements.txt           # Python dependencies
├── create_table.sql           # Table schema (reference/DR only — the callback creates it itself, see below)
└── README.md                  # This file
```

## What This Does

```
LLM request completed (via LiteLLM)
         |
CostTrackingCallback intercepts the response
         |
Reads input/output token counts from response usage
Cost computed via LiteLLM's built-in pricing (response_cost / completion_cost)
         |
Stores result in PostgreSQL (LiteLLM_Cost_Tracking table)
         |
Queryable via HTTP on port 4002
```

## How It Works

- Hooks into LiteLLM as a `CustomLogger` via `async_log_success_event`
- Reads `prompt_tokens` (input) and `completion_tokens` (output) from the response usage object
- Cost source: `kwargs["response_cost"]` (computed automatically by LiteLLM when the model is recognized in its pricing database), falling back to `litellm.completion_cost(...)`
- Extracts the user email from the `x-openwebui-user-email` header (forwarded by OpenWebUI)
- Exposes `GET /cost/latest?user=<email>` on port 4002 to query the latest cost entry

## Database

`litellm_cost_callback.py` creates its own table (`CREATE TABLE IF NOT EXISTS`, wrapped in a Postgres
advisory lock to stay safe with LiteLLM's multiple gunicorn workers) the first time it's imported —
no manual step needed, on a fresh Postgres volume or an already-initialized one. `create_table.sql`
in this folder is kept only as a reference schema for manual ops/DR (`psql` direct), not wired into
`docker-entrypoint-initdb.d` (that only fires on first Postgres init, already past on any deployed
instance).

```sql
CREATE TABLE IF NOT EXISTS "LiteLLM_Cost_Tracking" (
    id SERIAL PRIMARY KEY,
    user_email VARCHAR(255),
    chat_id VARCHAR(255),
    model VARCHAR(255),
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd FLOAT,
    timestamp TIMESTAMP DEFAULT NOW()
);
```

| Column        | Type      | Description                  |
|---------------|-----------|-------------------------------|
| user_email    | text      | User email from OpenWebUI    |
| chat_id       | text      | OpenWebUI conversation ID, used for session totals |
| model         | text      | Model name (e.g. claude-3-5) |
| input_tokens  | int       | Prompt token count           |
| output_tokens | int       | Completion token count       |
| cost_usd      | float     | Combined input+output cost ($) |
| timestamp     | timestamp | Auto-set on insert            |

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
    - "4001:4001"  # ecologits
    - "4002:4002"  # this callback
  volumes:
    - ./litellm-config.yaml:/app/config.yaml
    - ./cost-tracking/litellm_cost_callback.py:/app/litellm_cost_callback.py
    - ./ecologits/ecologits_callback.py:/app/ecologits_callback.py
```

Register the callback in `litellm-config.yaml`:

```yaml
litellm_settings:
  callbacks:
    - "litellm_cost_callback.proxy_handler_instance"
```

## Configuration

| Variable       | Description                        |
|----------------|------------------------------------|
| `DATABASE_URL` | PostgreSQL connection string (reuse LiteLLM's) |

## Displaying the Cost in OpenWebUI Conversations

This callback only computes and stores the cost — it does not show anything to the end user by itself. To display the cost of each prompt directly below the assistant's response in the OpenWebUI chat, install the **`cost_display.py`** Filter function from `../functions/` (see `../functions/README.md`).

That Filter calls this callback's `GET /cost/latest` endpoint after each response and appends the cost to the message. Without it installed and enabled in OpenWebUI (Admin Panel → Functions), the cost is tracked in the database but never surfaced in the chat UI.

## Notes

- If a model isn't in LiteLLM's pricing database, `response_cost` may be `None`/0 — the row is still written with `cost_usd = 0.0` so missing pricing data is visible rather than silently dropped.
- Coexists with `ecologits/` (tested together 24/09, see the root README's "Optional Features" section) — different port (4002 vs 4001) and a different table, both already merged into the shared `litellm` image.

---

**Version:** 1.0
**Last Updated:** 2026-06-17
