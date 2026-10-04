# OpenWebUI Functions

This folder contains custom functions that extend OpenWebUI's capabilities. These are deployed via the OpenWebUI admin panel (or installed automatically by the Complete installer). They are not separate Docker services.

## What are OpenWebUI Functions?

OpenWebUI supports three types of extensions:

- **Pipe** — acts as a custom model in the chat interface. When selected, it intercepts the user's message and routes it to an external service (e.g. n8n workflow) instead of a standard LLM.
- **Action** — adds a button below any message. When clicked, it triggers a specific operation.
- **Filter** — runs before/after a model response (e.g. cost or emissions display).

## How to install a function

1. Go to **Admin Panel → Functions**
2. Click **+** to add a new function
3. Paste the function code
4. Configure the **Valves** (settings) with your credentials and URLs
5. Enable the function

On **Complete** install (`./install.sh`), every `*.py` file in this folder is installed into OpenWebUI automatically, and the cost/emissions filters are marked global when those LiteLLM callbacks are enabled.

## Available Functions

### `n8n_pipe.py` — N8N Pipe
**Type:** Pipe (appears as a model in the chat)

Routes user messages to an n8n workflow via webhook and returns the workflow's response. Enables building custom AI agents and automations accessible directly from the chat interface.

**Valves to configure:**
| Valve | Description |
|-------|-------------|
| `n8n_url` | Webhook URL of the n8n workflow to trigger |
| `n8n_bearer_token` | Bearer token configured on the n8n webhook node |
| `input_field` | Field name for the user input (default: `chatInput`) |
| `response_field` | Field name for the workflow response (default: `output`) |

**Original author:** [Cole Medin](https://www.youtube.com/@ColeMedin)

### `cost_display.py`
**Type:** Filter

After each model response, calls the cost-tracking callback (`GET /cost/latest` on LiteLLM port 4002) and appends an estimated $ cost footer. Requires the LiteLLM cost callback to be enabled — see [`../cost-tracking/README.md`](../cost-tracking/README.md).

### `emissions_display.py`
**Type:** Filter

After each model response, calls the EcoLogits callback (`GET /impacts/latest` on LiteLLM port 4001) and appends carbon/energy footprint (per prompt + session total). Requires the EcoLogits callback — see [`../ecologits/README.md`](../ecologits/README.md).
