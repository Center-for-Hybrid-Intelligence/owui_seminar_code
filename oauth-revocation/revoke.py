#!/usr/bin/env python3
"""
OAuth Security: Disabled Azure AD Account Cleanup
Detects disabled Azure AD accounts and revokes their access in OpenWebUI and LiteLLM.

Usage:
  DRY_RUN=true python revoke.py   # preview only (default)
  DRY_RUN=false python revoke.py  # apply changes
"""

import os
import sys
import logging
import sqlite3

import msal
import requests

# Config
AZURE_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
AZURE_TENANT_ID = os.getenv("MICROSOFT_CLIENT_TENANT_ID")

OPENWEBUI_DB_PATH = os.getenv("OPENWEBUI_DB_PATH", "/openwebui-data/webui.db")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


def validate_config():
    missing = [
        v for v in [
            "MICROSOFT_CLIENT_ID",
            "MICROSOFT_CLIENT_SECRET",
            "MICROSOFT_CLIENT_TENANT_ID",
            "LITELLM_MASTER_KEY",
        ]
        if not os.getenv(v)
    ]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    if not os.path.exists(OPENWEBUI_DB_PATH):
        logger.error(f"OpenWebUI database not found at {OPENWEBUI_DB_PATH}")
        sys.exit(1)


def get_graph_token() -> str:
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_silent(GRAPH_SCOPES, account=None) or \
             app.acquire_token_for_client(scopes=GRAPH_SCOPES)

    if "access_token" not in result:
        logger.error(f"Failed to get Graph token: {result.get('error_description')}")
        sys.exit(1)
    return result["access_token"]


def get_disabled_azure_users(token: str) -> dict[str, dict]:
    """Returns disabled Azure AD users keyed by lowercase email."""
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        "https://graph.microsoft.com/v1.0/users"
        "?$filter=accountEnabled eq false"
        "&$select=id,mail,displayName"
    )
    disabled = {}
    while url:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        for user in data.get("value", []):
            email = (user.get("mail") or "").lower()
            if email:
                disabled[email] = user
        url = data.get("@odata.nextLink")

    logger.info(f"Found {len(disabled)} disabled Azure AD account(s)")
    return disabled


def get_owui_users_by_email() -> dict[str, dict]:
    """Returns OpenWebUI users keyed by lowercase email."""
    conn = None
    try:
        conn = sqlite3.connect(OPENWEBUI_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, name FROM user")
        users = {
            row["email"].lower(): dict(row)
            for row in cursor.fetchall()
            if row["email"]
        }
        logger.info(f"Found {len(users)} user(s) in OpenWebUI")
        return users
    except sqlite3.Error as e:
        logger.error(f"Failed to read OpenWebUI DB: {e}")
        return {}
    finally:
        if conn:
            conn.close()


def revoke_azure_sessions(token: str, azure_user_id: str, email: str):
    """Revoke all refresh tokens for the user in Azure AD."""
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would revoke Azure sessions for {email}")
        return
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{azure_user_id}/revokeSignInSessions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.ok:
        logger.info(f"Revoked Azure sessions for {email}")
    else:
        logger.error(f"Failed to revoke Azure sessions for {email}: {r.text}")


def delete_litellm_user(user_id: str, email: str):
    """Delete user from LiteLLM."""
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would delete LiteLLM user {email}")
        return
    r = requests.delete(
        f"{LITELLM_URL}/user/delete",
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        },
        json={"user_ids": [user_id]},
        timeout=10,
    )
    if r.ok:
        logger.info(f"Deleted LiteLLM user: {email}")
    else:
        logger.error(f"Failed to delete LiteLLM user {email}: {r.text}")


def main():
    validate_config()

    if DRY_RUN:
        logger.info("DRY RUN mode — no changes will be made")

    token = get_graph_token()
    disabled_azure = get_disabled_azure_users(token)
    owui_users = get_owui_users_by_email()

    affected = [email for email in disabled_azure if email in owui_users]

    if not affected:
        logger.info("No disabled Azure accounts found in OpenWebUI — nothing to do")
        return

    logger.info(f"{len(affected)} disabled Azure account(s) still active in OpenWebUI")

    for email in affected:
        owui_user = owui_users[email]
        azure_user = disabled_azure[email]
        logger.info(f"Processing: {email} (OpenWebUI ID: {owui_user['id']})")
        revoke_azure_sessions(token, azure_user["id"], email)
        delete_litellm_user(owui_user["id"], email)

    logger.info(f"Done — processed {len(affected)} account(s)")


if __name__ == "__main__":
    main()
