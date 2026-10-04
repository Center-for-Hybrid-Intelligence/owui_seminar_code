# AI Box — Self-Hosted AI Infrastructure

Internal AI platform built for the [Center for Hybrid Intelligence (CHI)](https://chi.au.dk) at Aarhus University. Powers Tech Circle workshops and internal AI experimentation.

## Stack Overview

```
┌─────────────────────────────────────────────────────────┐
│                      Users / Workshops                  │
└───────────────────────────┬─────────────────────────────┘
                            │
                    ┌───────▼────────┐
                    │   OpenWebUI    │  Chat interface (port 3001)
                    │  Microsoft SSO │
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │    LiteLLM     │  Model gateway (port 4000)
                    │   + Langfuse   │  Observability & cost tracking
                    └───────┬────────┘
                            │
          ┌─────────────────┼──────────────────┐
          │                 │                  │
    ┌─────▼──────┐  ┌───────▼──────┐  ┌───────▼──────┐
    │   OpenAI   │  │  Anthropic   │  │  Azure/Mistral│
    │  GPT / DALL│  │    Claude    │  │  + Ollama     │
    └────────────┘  └──────────────┘  └──────────────┘

    ┌────────────┐  ┌──────────────┐  ┌──────────────┐
    │    n8n     │  │     mcpo     │  │  user-sync   │
    │ Automation │  │  MCP servers │  │ OWUI↔LiteLLM │
    └────────────┘  └──────────────┘  └──────────────┘
```

## Services

| Service | Description | Port |
|---|---|---|
| `open-webui` | Chat interface with Microsoft SSO | 3001 |
| `litellm` | Unified LLM gateway (100+ providers) | 4000 |
| `langfuse` | LLM observability & cost tracing | 3002 |
| `n8n` | Workflow automation | 5679 |
| `mcpo` | MCP server proxy for OpenWebUI tools | 8001 |
| `user-sync` | Syncs users between OpenWebUI and LiteLLM | — |
| `postgres` | Database backend (n8n + LiteLLM) | 5434 |
| `ollama` | Local LLM hosting | — |
| `cost-tracking` *(optional)* | Per-message $ cost tracking (LiteLLM callback) | 4002 |
| `ecologits` *(optional)* | Per-message carbon/energy footprint (LiteLLM callback) | 4001 |
| `oauth-revocation` *(optional, on-demand)* | Revokes access for disabled Azure AD accounts | — |

Document uploads (PDF, DOCX, etc.) use Open WebUI’s built-in extractors. There is no Docling service in this stack.

## Optional Features

Each optional feature lives in its own folder with its own `README.md` (linked below) — read that file
for what it does, how it's configured, and its own troubleshooting section before enabling it.

| Folder | What it adds | How to enable |
|---|---|---|
| [`cost-tracking/`](cost-tracking/README.md) | Tracks $ cost per LLM call, footer in chat via `functions/cost_display.py` | Add `"litellm_cost_callback.proxy_handler_instance"` to `callbacks:` in `litellm-config.yaml`, rebuild `litellm` |
| [`ecologits/`](ecologits/README.md) | Tracks carbon/energy footprint per LLM call, footer in chat via `functions/emissions_display.py` | Add `"ecologits_callback.proxy_handler_instance"` to `callbacks:` in `litellm-config.yaml`, rebuild `litellm` |
| [`oauth-revocation/`](oauth-revocation/README.md) | Revokes OWUI/LiteLLM access for disabled Azure AD accounts | On-demand: `docker compose --profile oauth-revocation run --rm oauth-revocation` (`DRY_RUN=true` by default) |
| [`user-sync/`](user-sync/README.md) | Provisions a LiteLLM Team (budget + virtual key + restricted connection) per OpenWebUI group | Always on — part of the base stack |

Both LLM-tracking callbacks are built into the same `litellm` image (see `litellm/Dockerfile`) and are
inert until listed in `litellm-config.yaml`'s `callbacks: []`. They combine freely — enabling both at
once (tested 24/09):

```yaml
# litellm-config.yaml
litellm_settings:
  callbacks: ["litellm_cost_callback.proxy_handler_instance", "ecologits_callback.proxy_handler_instance"]
```

then `docker compose up -d --build litellm` — both footers (🟠 and 🟢) appear on the same message,
each callback writing to its own table (`LiteLLM_Cost_Tracking`, `EcoLogits_Impacts`), no conflict.

## Supported Models

**Text**
- OpenAI: GPT-3.5, GPT-5.2, GPT-5.1 (Azure)
- Anthropic: Claude Haiku 4.5, Opus 4.5, Sonnet 4.6 (with extended thinking)
- Mistral Small (Azure)
- Ollama: Llama 3.2 and any locally hosted model

**Image generation**
- DALL-E 2, DALL-E 3, GPT Image 1

**Audio**
- Whisper-1 (STT), TTS-1 (TTS)

## Quick Start

Published ports bind to `127.0.0.1` unless `BIND_HOST=0.0.0.0` in `.env`.

### Easy install (recommended)

Requirements: **Linux**, **Docker** installed and running, **Python 3**, and **one API key** from your AI provider.

1. Unzip (or clone) the project.
2. Open a terminal in that folder.
3. Run:

```bash
./install.sh
```

This opens the **GenAI Stack** installer in your browser (leave the terminal open). The UI shows:

- **Level 0 — Not installed**
- **Level 1 — Standard** — postgres, LiteLLM, OpenWebUI, user-sync  
- **Level 2 — Complete** — standard plus mcpo, n8n, langfuse, and OpenWebUI functions from `functions/`

It checks requirements automatically. Use **Configure** to paste an API key (saved only in a local `.env`). Then click **Install**. Progress and fix-it prompts appear in the UI if something is missing.

For SSH or automation, use the terminal menu instead:

```bash
./install.sh --cli
```

Run `./install.sh` again anytime to **repair** or upgrade to the next level — it does not wipe your data. It reuses known volumes (`genaistack_*` / `owui_*`), keeps or creates `ollama_network`, moves off busy ports, and fixes common password mismatches.

You can edit `.env` later for ports, SSO, extra keys, and other settings. You do **not** need to export environment variables in the terminal.

Wipe containers with `./erase.sh` (keeps chat/DB volumes). Full data wipe: `./erase.sh --wipe-data`. Add `--also-network` to drop `ollama_network` too. Then run `./install.sh` again.

### What gets started

| Profile | Services |
|---|---|
| Standard | `postgres`, `litellm`, `open-webui`, `user-sync` |
| Complete | above + `mcpo`, `n8n`, `langfuse`, and installs `functions/*.py` into OpenWebUI |

Optional service still not started by the installer: `ollama`. After a successful install you can start it yourself if needed:

```bash
docker network create ollama_network   # only if missing
docker compose up -d ollama
```

Complete creates the `langfuse` database and starts Langfuse. It also enables LiteLLM cost + emissions callbacks (ports 4001/4002) so the OpenWebUI display filters work, and marks those filters as global. LiteLLM→Langfuse tracing is still off until you add project keys (see [Langfuse setup](#langfuse-setup)).

### Manual install

Same stack without the installer UI. Prefer `./install.sh` unless you know Compose.

```bash
git clone https://github.com/Center-for-Hybrid-Intelligence/owui.git
cd owui
cp .env.example .env
# set at least one provider API key; leave secrets as auto where noted
docker network create ollama_network
docker compose up -d postgres
# wait until postgres is healthy, then:
docker compose up -d --build --no-deps litellm open-webui user-sync
# optional: docker compose up -d mcpo n8n langfuse ollama
```

Create the OpenWebUI admin in the UI (first account is admin). Copy that account's API key (Settings → Account) into `.env` as `OPENWEBUI_ADMIN_API_KEY`, create a group, add users, then:

```bash
docker compose up -d --force-recreate --no-deps user-sync
```

Do not point OpenWebUI's OpenAI connection at LiteLLM with `LITELLM_MASTER_KEY`. That key is unrestricted and skips per-group budgets. `user-sync` adds one connection per group, using a team virtual key.

Replace every `auto` value yourself with a URL-safe secret (`openssl rand -hex 24`) if you are not using `./install.sh`. A password that contains `/`, `@`, or `:` breaks the database URL.

See `.env.example` for the full variable list. Microsoft SSO, Langfuse keys, and similar options are documented there and in the sections below.

#### Langfuse database (only if you start Langfuse)

The `litellm` database and user are created automatically via `init-db.sh` on first Postgres boot. Create the Langfuse database once:

```bash
docker compose up -d postgres
docker exec -it postgres psql -U n8n -c "CREATE DATABASE langfuse;"
```

### Users and groups

Access is per OpenWebUI **group**, not per user. Being an admin alone does not grant models.

**After `./install.sh`:** a group named `default` already exists with the admin as a member, and user-sync has provisioned its LiteLLM team and models (shown as `default.…`). You can create more groups for other budgets or model sets.

**Manual install:** after first boot the model list is empty until you create a group.

1. Sign in at http://localhost:3001 as the admin (`WEBUI_ADMIN_EMAIL` / `WEBUI_ADMIN_PASSWORD` from `.env` if you used `./install.sh`).
2. **Admin Panel → Users** — create accounts, or let people in through Microsoft SSO if configured. Public signup is off after `./install.sh`.
3. **Admin Panel → Groups** — create a group and add the users who should share a budget and a model list (or use `default`).
4. Restart sync so a new group is provisioned immediately (otherwise it waits for `USER_SYNC_INTERVAL`):

```bash
docker compose restart user-sync
docker compose logs -f user-sync
```

`user-sync` creates a LiteLLM Team for that group (`DEFAULT_TEAM_BUDGET`), a virtual key, and an OpenWebUI connection prefixed with the group name. Members see models as `GroupName.gpt-5.4`, and only members of that group can use them. Two groups means two budgets and two model lists.

The team is created with every model allowed. To give a group a smaller set, open http://localhost:4000/ui (user `LITELLM_UI_USERNAME`, password `LITELLM_UI_PASSWORD`) → **Teams** → that team → **Models**. A model left off the list is rejected by LiteLLM. `user-sync` does not refresh that list after the group is first created.

### Langfuse setup

1. Go to `http://localhost:3002`
2. Create an admin account
3. Create a project named `ai-box`
4. Go to **Settings → API Keys** → generate a key pair
5. Add the keys to your `.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`)
6. Restart LiteLLM: `docker compose restart litellm`

## Accessing the Platform

| Interface | URL |
|---|---|
| OpenWebUI | http://localhost:3001 |
| LiteLLM Dashboard | http://localhost:4000/ui |
| Langfuse | http://localhost:3002 |
| n8n | http://localhost:5679 |

## Key Configuration Files

- `install.sh` — opens the browser installer (or `--cli` for the terminal menu)
- `installer-ui/` — local graphical installer (Python stdlib server + static UI)
- `VERSION` — package version shown in the installer
- `genaistack/` — installer image and setup logic
- `docker-compose.yml` — service definitions
- `litellm-config.yaml` — model list, routing, callbacks (source of truth; easy install derives a lean runtime copy under `genaistack/`)
- `init-db.sh` — auto-creates the `litellm` database and user on first postgres boot (reads `LITELLM_DB_PASSWORD` from env)
- `mcpo/` — optional local Dockerfile (compose uses `ghcr.io/open-webui/mcpo` by default)
- `functions/` — custom OpenWebUI functions (see [`functions/README.md`](functions/README.md))
- `erase.sh` — tear down containers (keeps volumes unless `--wipe-data`)

Every folder above that's its own feature (not just a config file) carries its own `README.md` with
the detail this file doesn't repeat — package contents, how it works, its own troubleshooting. This
file only gives the map: [`user-sync/`](user-sync/README.md), [`cost-tracking/`](cost-tracking/README.md),
[`ecologits/`](ecologits/README.md), [`oauth-revocation/`](oauth-revocation/README.md). Each of those
READMEs links back here at the top.

## Observability

Langfuse traces every LLM call through LiteLLM. Each trace includes:
- Exact prompt and response
- Token counts (input / output)
- Estimated cost per call
- Latency
- User identity (passed from OpenWebUI via `X-OpenWebUI-User-Email` header)

## Troubleshooting

**LiteLLM loads a stale API key**
Your shell session may have an env var overriding the `.env`. Fix:
```bash
unset OPENAI_API_KEY  # or whichever key
docker compose down
docker compose up -d --force-recreate
```

**Langfuse not receiving traces**
Check that `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST=http://langfuse:3000` are set in the `litellm` service env, and that `success_callback: ["langfuse"]` is in `litellm-config.yaml`.

**Database connection issues**
```bash
docker logs postgres
docker exec -it postgres psql -U n8n -d n8n
```

**`network ollama_network not found` on `docker compose up`**
The network is declared `external` in `docker-compose.yml` and isn't created automatically by Compose alone. `./install.sh` creates it for you.
Fix: `docker network create ollama_network`, then retry.

**A service won't start / env var silently defaults to blank**
Usually a missing key in `.env` that exists in `docker-compose.yml`'s `environment:` list (Compose
warns `variable is not set, defaulting to a blank string` but still starts the container). Compare
your `.env` against the current `.env.example` — a `git pull` can add new variables that an existing
local `.env` doesn't have yet.

**LiteLLM can't connect to Postgres / auth failed for user "litellm"**
`LITELLM_DB_PASSWORD` in `.env` must match what was used when the `litellm` Postgres user was first
created (`init-db.sh` runs only once, on first volume init — changing the password in `.env` later
does **not** update it in Postgres). Fix: either reset it to the original value, or update it in
Postgres directly (`ALTER USER litellm WITH PASSWORD '...'`) and match `.env` to that.

## Contributing

1. Clone the repo
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Make your changes
4. Open a pull request on `main`

Never commit `.env` — it is gitignored.

## Built With

- [OpenWebUI](https://github.com/open-webui/open-webui)
- [LiteLLM](https://github.com/BerriAI/litellm)
- [Langfuse](https://langfuse.com)
- [n8n](https://n8n.io)
- [Ollama](https://ollama.ai)
- [PostgreSQL](https://postgresql.org)