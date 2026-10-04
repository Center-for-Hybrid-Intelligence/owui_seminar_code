#!/usr/bin/env python3
"""
OpenWebUI to LiteLLM User Synchronization Service
Automatically creates internal users in LiteLLM when new users are created in OpenWebUI.
Provisions one LiteLLM Team per OpenWebUI group, each with its own budget, virtual key,
prefixed OpenWebUI connection, and Private+group access on its models.
Reads directly from OpenWebUI's SQLite database.
"""

import os
import sys
import time
import logging
import requests
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

# Configuration from environment variables
OPENWEBUI_DB_PATH = os.getenv("OPENWEBUI_DB_PATH", "/openwebui-data/webui.db")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY")
OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "http://open-webui:8080")
OPENWEBUI_ADMIN_API_KEY = os.getenv("OPENWEBUI_ADMIN_API_KEY")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "60"))  # seconds
DEFAULT_USER_ROLE = os.getenv("DEFAULT_USER_ROLE", "internal_user")
DEFAULT_TEAM_BUDGET = float(os.getenv("DEFAULT_TEAM_BUDGET", "500"))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class UserSyncService:
    def __init__(self):
        self.validate_config()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"

        self.owui_session = requests.Session()
        self.owui_session.headers["Authorization"] = f"Bearer {OPENWEBUI_ADMIN_API_KEY}"

    def validate_config(self):
        """Validate required environment variables"""
        if not LITELLM_MASTER_KEY:
            logger.error("LITELLM_MASTER_KEY is not set!")
            sys.exit(1)

        if not OPENWEBUI_ADMIN_API_KEY:
            logger.error("OPENWEBUI_ADMIN_API_KEY is not set!")
            sys.exit(1)

        if not os.path.exists(OPENWEBUI_DB_PATH):
            logger.error(f"OpenWebUI database not found at {OPENWEBUI_DB_PATH}")
            logger.error("Make sure the OpenWebUI data volume is mounted correctly")
            sys.exit(1)

        logger.info("Configuration validated successfully")
        logger.info(f"OpenWebUI DB: {OPENWEBUI_DB_PATH}")
        logger.info(f"LiteLLM URL: {LITELLM_URL}")
        logger.info(f"OpenWebUI URL: {OPENWEBUI_URL}")
        logger.info(f"Sync interval: {SYNC_INTERVAL}s")

    # ------------------------------------------------------------------
    # OpenWebUI DB reads
    # ------------------------------------------------------------------

    def get_openwebui_users(self) -> List[Dict]:
        """Fetch all users from OpenWebUI SQLite database"""
        conn = None
        try:
            conn = sqlite3.connect(OPENWEBUI_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT id, email, name, role FROM user")
            rows = cursor.fetchall()

            users = []
            for row in rows:
                users.append({
                    "id": row["id"],
                    "email": row["email"] if row["email"] else f"{row['id']}@local",
                    "name": row["name"] if row["name"] else "Unknown",
                    "role": row["role"] if row["role"] else "user"
                })

            logger.debug(f"Found {len(users)} users in OpenWebUI database")
            return users

        except sqlite3.Error as e:
            logger.error(f"Failed to read OpenWebUI database: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error reading OpenWebUI users: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_openwebui_groups(self) -> List[Dict]:
        """Fetch all groups and their members from OpenWebUI SQLite database"""
        conn = None
        try:
            conn = sqlite3.connect(OPENWEBUI_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT id, name FROM 'group'")
            groups = [{"id": row["id"], "name": row["name"], "user_ids": []} for row in cursor.fetchall()]

            try:
                cursor.execute("SELECT group_id, user_id FROM group_member")
                memberships = cursor.fetchall()
                group_map = {g["id"]: g for g in groups}
                for row in memberships:
                    if row["group_id"] in group_map:
                        group_map[row["group_id"]]["user_ids"].append(row["user_id"])
            except sqlite3.OperationalError:
                cursor.execute("SELECT id, user_ids FROM 'group'")
                import json
                for row in cursor.fetchall():
                    for g in groups:
                        if g["id"] == row["id"] and row["user_ids"]:
                            g["user_ids"] = json.loads(row["user_ids"])

            logger.debug(f"Found {len(groups)} groups in OpenWebUI database")
            return groups

        except sqlite3.Error as e:
            logger.error(f"Failed to read OpenWebUI groups: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error reading OpenWebUI groups: {e}")
            return []
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # LiteLLM: users
    # ------------------------------------------------------------------

    def get_litellm_users(self) -> Dict[str, Dict]:
        """Fetch all users from LiteLLM and return as a dict keyed by user_id"""
        try:
            response = self.session.get(
                f"{LITELLM_URL}/user/list",
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            users = {}
            if isinstance(data, dict) and "users" in data:
                for user in data["users"]:
                    users[user.get("user_id")] = user

            logger.debug(f"Found {len(users)} users in LiteLLM")
            return users
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch LiteLLM users: {e}")
            return {}

    def create_litellm_user(self, user_data: Dict) -> Optional[Dict]:
        """Create a new internal user in LiteLLM (no individual budget - budget lives on the Team)"""
        try:
            payload = {
                "user_id": user_data["id"],
                "user_email": user_data.get("email", f"{user_data['id']}@local"),
                "user_role": DEFAULT_USER_ROLE,
                "models": ["*"],
                "auto_create_key": False,
                "metadata": {
                    "synced_from": "openwebui",
                    "synced_at": datetime.utcnow().isoformat(),
                    "openwebui_name": user_data.get("name", "Unknown")
                }
            }

            response = self.session.post(
                f"{LITELLM_URL}/user/new",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            result = response.json()

            logger.info(f"✅ Created LiteLLM user: {user_data.get('email')} (ID: {user_data['id']})")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to create LiteLLM user {user_data.get('email')}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    def update_litellm_user(self, user_id: str, user_data: Dict) -> bool:
        """Propagate an OpenWebUI name/email change to the matching LiteLLM internal user"""
        try:
            payload = {
                "user_id": user_id,
                "user_email": user_data.get("email"),
                "metadata": {
                    "synced_from": "openwebui",
                    "synced_at": datetime.utcnow().isoformat(),
                    "openwebui_name": user_data.get("name", "Unknown")
                }
            }
            response = self.session.post(
                f"{LITELLM_URL}/user/update",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"✏️ Updated LiteLLM user: {user_data.get('email')} (ID: {user_id})")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to update LiteLLM user {user_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    # ------------------------------------------------------------------
    # LiteLLM: teams + virtual keys
    # ------------------------------------------------------------------

    def get_litellm_teams(self) -> List[Dict]:
        """Fetch all teams from LiteLLM (v2 list is paginated; default page_size is 10)."""
        try:
            team_list: List[Dict] = []
            page = 1
            while True:
                response = self.session.get(
                    f"{LITELLM_URL}/v2/team/list",
                    params={"page": page, "page_size": 100},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list):
                    return data
                batch = data.get("teams") or data.get("data") or []
                team_list.extend(batch)
                total_pages = int(data.get("total_pages") or 1)
                if page >= total_pages or not batch:
                    break
                page += 1
                if page > 200:
                    logger.warning("Stopped team list pagination after 200 pages")
                    break
            logger.debug(f"Found {len(team_list)} teams in LiteLLM")
            return team_list
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch LiteLLM teams: {e}")
            return []

    def create_litellm_team(self, group: Dict) -> Optional[str]:
        """Create a new team in LiteLLM, returns team_id.
        Model allow-list is intentionally left unset here - restricting which models a
        Team may use is a manual admin decision (LiteLLM UI -> Teams -> Models)."""
        try:
            payload = {
                "team_alias": group["name"],
                "max_budget": DEFAULT_TEAM_BUDGET,
                "metadata": {
                    "synced_from": "openwebui",
                    "openwebui_group_id": group["id"],
                    "synced_at": datetime.utcnow().isoformat()
                }
            }
            response = self.session.post(
                f"{LITELLM_URL}/team/new",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            team_id = response.json().get("team_id")
            logger.info(f"✅ Created LiteLLM team: {group['name']} (ID: {team_id})")
            return team_id
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to create LiteLLM team '{group['name']}': {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    def update_litellm_team_alias(self, team_id: str, new_alias: str) -> bool:
        """Rename an existing Team's alias in place (used when its OpenWebUI group is renamed)"""
        try:
            response = self.session.post(
                f"{LITELLM_URL}/team/update",
                headers={"Content-Type": "application/json"},
                json={"team_id": team_id, "team_alias": new_alias},
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"✏️ Renamed LiteLLM team {team_id} -> '{new_alias}'")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to rename team {team_id} to '{new_alias}': {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def generate_team_key(self, team_id: str) -> Optional[str]:
        """Generate a virtual key scoped to a Team. The plaintext key is only ever returned
        once by LiteLLM here - it cannot be retrieved again afterwards."""
        try:
            response = self.session.post(
                f"{LITELLM_URL}/key/generate",
                headers={"Content-Type": "application/json"},
                json={"team_id": team_id, "metadata": {"synced_from": "openwebui"}},
                timeout=10
            )
            response.raise_for_status()
            key = response.json().get("key")
            logger.info(f"🔑 Generated virtual key for team {team_id}")
            return key
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to generate key for team {team_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    # ------------------------------------------------------------------
    # OpenWebUI: connections + model access
    # ------------------------------------------------------------------

    def get_openwebui_openai_config(self) -> Optional[Dict]:
        """GET the current OpenAI-compatible connections config from OpenWebUI"""
        try:
            response = self.owui_session.get(f"{OPENWEBUI_URL}/openai/config", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch OpenWebUI OpenAI config: {e}")
            return None

    def update_openwebui_openai_config(self, base_urls: List[str], keys: List[str], api_configs: Dict) -> bool:
        """POST the full replacement connections config to OpenWebUI (not an append - see /openai/config/update)"""
        try:
            response = self.owui_session.post(
                f"{OPENWEBUI_URL}/openai/config/update",
                headers={"Content-Type": "application/json"},
                json={
                    "ENABLE_OPENAI_API": True,
                    "OPENAI_API_BASE_URLS": base_urls,
                    "OPENAI_API_KEYS": keys,
                    "OPENAI_API_CONFIGS": api_configs,
                },
                timeout=15
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update OpenWebUI OpenAI config: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def sync_model_access(self, group: Dict, litellm_key: str):
        """Set Private+group access_grants on every model exposed under this group's prefixed
        connection, so only members of the matching OpenWebUI group can see/use them."""
        try:
            response = requests.get(
                f"{LITELLM_URL}/v1/models",
                headers={"Authorization": f"Bearer {litellm_key}"},
                timeout=10
            )
            response.raise_for_status()
            models = response.json().get("data", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to list models for team key ('{group['name']}'): {e}")
            return

        updated = 0
        for model in models:
            model_id = model.get("id")
            if not model_id:
                continue
            prefixed_id = f"{group['name']}.{model_id}"
            try:
                response = self.owui_session.post(
                    f"{OPENWEBUI_URL}/api/v1/models/model/access/update",
                    headers={"Content-Type": "application/json"},
                    json={
                        "id": prefixed_id,
                        "access_grants": [
                            {"principal_type": "group", "principal_id": group["id"], "permission": "read"}
                        ],
                    },
                    timeout=10
                )
                response.raise_for_status()
                updated += 1
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Failed to set access grants on '{prefixed_id}': {e}")

        if updated:
            logger.info(f"🔒 Set Private+group access on {updated} model(s) for '{group['name']}'")

    def rename_openwebui_connection(self, old_prefix: str, new_prefix: str) -> Optional[str]:
        """Rename an existing connection's prefix_id in place, keeping its key (no new key
        generated, no orphaned connection left behind). Returns the connection's key on
        success, so the caller can refresh model access grants under the new prefix - or
        None if no connection matched the old prefix."""
        config = self.get_openwebui_openai_config()
        if config is None:
            return None

        base_urls = list(config.get("OPENAI_API_BASE_URLS", []))
        keys = list(config.get("OPENAI_API_KEYS", []))
        api_configs = dict(config.get("OPENAI_API_CONFIGS", {}))

        matched_key = None
        for idx, cfg in api_configs.items():
            if isinstance(cfg, dict) and cfg.get("prefix_id") == old_prefix:
                cfg["prefix_id"] = new_prefix
                try:
                    matched_key = keys[int(idx)]
                except (ValueError, IndexError):
                    matched_key = None
                break

        if matched_key is None:
            return None

        if not self.update_openwebui_openai_config(base_urls, keys, api_configs):
            return None

        return matched_key

    def ensure_team_provisioned(self, group: Dict, team_id: str) -> bool:
        """Ensure this Team has its own virtual key connected to OpenWebUI (prefixed with the
        group's name), with Private+group access applied to every model behind it.
        The key is generated lazily - only if no matching connection exists yet.
        Returns True if the connection was just created (i.e. this run provisioned it)."""
        config = self.get_openwebui_openai_config()
        if config is None:
            return False

        base_urls = list(config.get("OPENAI_API_BASE_URLS", []))
        keys = list(config.get("OPENAI_API_KEYS", []))
        api_configs = dict(config.get("OPENAI_API_CONFIGS", {}))

        for cfg in api_configs.values():
            if isinstance(cfg, dict) and cfg.get("prefix_id") == group["name"]:
                return False  # already connected

        litellm_key = self.generate_team_key(team_id)
        if not litellm_key:
            return False

        new_idx = str(len(base_urls))
        base_urls.append(f"{LITELLM_URL}/v1")
        keys.append(litellm_key)
        api_configs[new_idx] = {"enable": True, "prefix_id": group["name"]}

        if not self.update_openwebui_openai_config(base_urls, keys, api_configs):
            return False

        logger.info(f"🔌 Created OpenWebUI connection for '{group['name']}' (prefix: {group['name']}.)")
        self.sync_model_access(group, litellm_key)
        return True

    # ------------------------------------------------------------------
    # Sync loops
    # ------------------------------------------------------------------

    def sync_groups(self):
        """Sync OpenWebUI groups to LiteLLM Teams: create/rename by stable group id (not by
        name), and make sure each Team has its own virtual key + prefixed OpenWebUI connection
        + Private/group access on its models."""
        logger.info("🔄 Starting group synchronization...")

        groups = self.get_openwebui_groups()
        if not groups:
            logger.warning("No groups found in OpenWebUI or connection failed")
            return

        litellm_teams = self.get_litellm_teams()
        teams_by_group_id = {
            t.get("metadata", {}).get("openwebui_group_id"): t
            for t in litellm_teams
            if t.get("metadata", {}).get("openwebui_group_id")
        }

        for group in groups:
            team = teams_by_group_id.get(group["id"])

            if team is None:
                team_id = self.create_litellm_team(group)
                if not team_id:
                    continue
                provisioned = self.ensure_team_provisioned(group, team_id)
                logger.info(
                    f"✨ Team '{group['name']}' created and provisioned" if provisioned
                    else f"✅ Team '{group['name']}' created"
                )
                continue

            team_id = team.get("team_id")
            old_alias = team.get("team_alias")

            if old_alias and old_alias != group["name"]:
                self.update_litellm_team_alias(team_id, group["name"])
                # Reuse the existing key/connection under the new prefix instead of
                # generating a new key and leaving the old connection orphaned.
                litellm_key = self.rename_openwebui_connection(old_alias, group["name"])
                if litellm_key:
                    logger.info(f"🔀 Renamed OpenWebUI connection '{old_alias}.' -> '{group['name']}.'")
                    self.sync_model_access(group, litellm_key)
                else:
                    # No connection existed under the old name either - provision fresh
                    if self.ensure_team_provisioned(group, team_id):
                        logger.info(f"✨ Team '{group['name']}': connection + model access provisioned")
            else:
                if self.ensure_team_provisioned(group, team_id):
                    logger.info(f"✨ Team '{group['name']}': connection + model access provisioned")
                else:
                    logger.info(f"✓ Team '{group['name']}': already up to date")

        logger.info("Group sync complete")

    def sync_users(self):
        """Main sync logic: create missing users in LiteLLM, update ones whose name/email
        changed in OpenWebUI"""
        logger.info("🔄 Starting user synchronization...")

        openwebui_users = self.get_openwebui_users()
        litellm_users = self.get_litellm_users()

        if not openwebui_users:
            logger.warning("No users found in OpenWebUI or connection failed")
            return

        created_count = 0
        updated_count = 0
        for user in openwebui_users:
            user_id = user.get("id")
            if not user_id:
                continue

            if user_id not in litellm_users:
                logger.info(f"🆕 New user detected: {user.get('email', user_id)}")
                if self.create_litellm_user(user):
                    created_count += 1
                continue

            existing = litellm_users[user_id]
            current_email = existing.get("user_email")
            current_name = (existing.get("metadata") or {}).get("openwebui_name")
            if current_email != user.get("email") or current_name != user.get("name"):
                if self.update_litellm_user(user_id, user):
                    updated_count += 1

        if created_count or updated_count:
            logger.info(f"✨ Sync complete: {created_count} new user(s), {updated_count} updated")
        else:
            logger.info("✓ Sync complete: All users already synchronized")

    def run(self):
        """Main loop: run sync periodically"""
        logger.info("🚀 User Sync Service started")
        logger.info(f"Default team budget: ${DEFAULT_TEAM_BUDGET}")

        self.sync_users()
        self.sync_groups()

        while True:
            try:
                time.sleep(SYNC_INTERVAL)
                self.sync_users()
                self.sync_groups()
            except KeyboardInterrupt:
                logger.info("Shutting down gracefully...")
                break
            except Exception as e:
                logger.error(f"Unexpected error in sync loop: {e}")
                time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    service = UserSyncService()
    service.run()
