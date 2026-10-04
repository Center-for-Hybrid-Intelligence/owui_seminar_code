[← back to main README](../README.md)

# OAuth Revocation

On-demand script that detects disabled Azure AD accounts and revokes their access in OpenWebUI and LiteLLM.

## Package Contents

```
oauth-revocation/
├── revoke.py              # Main script (Python)
├── Dockerfile             # Docker image builder
├── requirements.txt       # Runtime dependencies
├── requirements-dev.txt   # Dev dependencies (pytest)
├── test_revoke.py         # Unit tests
└── README.md              # This file
```

## What This Does

When an employee leaves, disabling their Azure AD account does not automatically invalidate active OAuth sessions in OpenWebUI. This script closes that gap.

```
Azure AD account disabled
         |
Script detects disabled accounts
         |
Cross-references with OpenWebUI users
         |
For each match:
  ├── Revokes Azure sessions (revokeSignInSessions)
  └── Deletes user from LiteLLM
```

## Quick Start

```bash
# 1. Build the image
docker build -t oauth-revocation .

# 2. Preview (DRY_RUN enabled by default — no changes applied)
docker run --env-file ../.env \
  -e OPENWEBUI_DB_PATH=/openwebui-data/webui.db \
  -v open_webui_data:/openwebui-data:ro \
  oauth-revocation

# 3. Apply changes
docker run --env-file ../.env \
  -e OPENWEBUI_DB_PATH=/openwebui-data/webui.db \
  -e DRY_RUN=false \
  -v open_webui_data:/openwebui-data:ro \
  oauth-revocation
```

## Configuration

All configuration via environment variables:

```bash
# Azure AD (reuse values from .env)
MICROSOFT_CLIENT_ID=your-client-id
MICROSOFT_CLIENT_SECRET=your-client-secret
MICROSOFT_CLIENT_TENANT_ID=your-tenant-id

# LiteLLM (reuse values from .env)
LITELLM_MASTER_KEY=sk-xxx
LITELLM_URL=http://litellm:4000          # default

# OpenWebUI
OPENWEBUI_DB_PATH=/openwebui-data/webui.db  # default

# Safety
DRY_RUN=true                             # default — set to false to apply changes
```

## Azure App Registration

The script requires an Azure App Registration with the following **application permissions** (not delegated):

- `User.Read.All` — to list disabled accounts
- `User.RevokeSessions.All` — to revoke sign-in sessions

## How It Works

1. Authenticates with Microsoft Graph API using client credentials
2. Fetches all disabled Azure AD accounts
3. Cross-references with OpenWebUI users (via SQLite)
4. For each match:
   - Calls `POST /users/{id}/revokeSignInSessions` on Microsoft Graph
   - Calls `DELETE /user/delete` on LiteLLM

## Running Tests

Unit tests cover all functions with mocked external calls (no stack required).

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest test_revoke.py -v
```

## Troubleshooting

**Missing env vars:**
The script will exit immediately and list the missing variables.

**OpenWebUI DB not found:**
Make sure the `open_webui_data` volume is mounted correctly.

**Graph API 403:**
Check that the App Registration has the required permissions and that admin consent has been granted.

---

**Version:** 1.1
**Last Updated:** 2026-05-20
