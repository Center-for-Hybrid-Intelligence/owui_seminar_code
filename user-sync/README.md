[← back to main README](../README.md)

# User Sync Service

Background service that keeps LiteLLM in sync with OpenWebUI: creates/updates users, and
provisions one LiteLLM Team per OpenWebUI group — each with its own budget, virtual key,
prefixed OpenWebUI connection, and `Private+group` access on its models.

## Package Contents

```
user-sync/
├── sync_users.py          # Main sync service (Python)
├── Dockerfile             # Docker image builder
├── requirements.txt       # Runtime dependencies
├── requirements-dev.txt   # Dev dependencies (pytest)
├── test_sync_users.py     # Unit tests
├── .dockerignore
└── README.md              # This file
```

## What This Does

```
OpenWebUI users/groups (read from SQLite, every SYNC_INTERVAL)
         |
    ┌────┴────┐
    |         |
 Users     Groups
    |         |
Create/update  For each group without a matching Team:
LiteLLM users  ├─ Create a LiteLLM Team (budget = DEFAULT_TEAM_BUDGET)
(no individual ├─ Generate a Team-scoped virtual key
 budget - it's  ├─ Create an OpenWebUI connection prefixed "<group>." using that key
 on the Team)   └─ Set Private+group access_grants on every model behind it
```

Groups are matched to Teams by a stable `openwebui_group_id` in the Team's metadata, not by
name — renaming a group in OpenWebUI renames the Team and reuses the existing key/connection
(no orphaned connection, no new key generated).

## Configuration

Set values in the repo-root `.env` (see `.env.example`). Compose maps them into the container:

| In `.env` | Inside the container | Notes |
|---|---|---|
| `USER_SYNC_INTERVAL` | `SYNC_INTERVAL` | Seconds between sync cycles (default `60`) |
| `DEFAULT_TEAM_BUDGET` | `DEFAULT_TEAM_BUDGET` | `$` per Team / OpenWebUI group (`.env.example` = `20`; Compose default if unset = `500`) |
| `DEFAULT_USER_ROLE` | `DEFAULT_USER_ROLE` | Default `internal_user` |
| `OPENWEBUI_ADMIN_API_KEY` | same | Required — OpenWebUI admin API key (Settings → Account) |
| `LITELLM_MASTER_KEY` | same | Already required by LiteLLM |

Other container defaults (override only if you change Compose networking):

```bash
LITELLM_URL=http://litellm:4000
OPENWEBUI_URL=http://open-webui:8080
OPENWEBUI_DB_PATH=/openwebui-data/webui.db
```

`OPENWEBUI_ADMIN_API_KEY` is required — unlike plain user sync (SQLite-only), provisioning
Team connections and model access grants calls OpenWebUI's admin API.

## How It Works

1. Reads users and groups from OpenWebUI's SQLite database (read-only mount)
2. Creates any user missing in LiteLLM (no budget — budget lives on the Team); updates
   name/email if changed
3. For each group: finds or creates its LiteLLM Team, ensures it has a virtual key connected
   to OpenWebUI under a `<group-name>.` prefix, and sets `Private+group` access on every
   model exposed through that connection
4. Repeats every `SYNC_INTERVAL` seconds

## Monitoring

```bash
docker compose logs -f user-sync
docker compose ps user-sync
```

## Running Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest test_sync_users.py -v
```

## Troubleshooting

**Service exits immediately:** check for "is not set!" in the logs — `LITELLM_MASTER_KEY` or
`OPENWEBUI_ADMIN_API_KEY` missing, or the OpenWebUI DB volume not mounted.

**A group never gets provisioned:** check `docker compose logs user-sync` for `❌` lines —
most failures are OpenWebUI/LiteLLM API errors (bad admin key, LiteLLM not reachable yet).

**Full reset:**
```bash
docker compose down user-sync
docker compose build --no-cache user-sync
docker compose up -d user-sync
```

---

**Version:** 2.0 (Team-based budget/restriction by group)
**Last Updated:** 2026-10-04
