"""
Tests for sync_users.py — OpenWebUI <-> LiteLLM user/group sync service.
Run with: pytest test_sync_users.py -v
"""

import os
import sqlite3
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path: str, users: list[dict] | None = None, groups: list[dict] | None = None,
             memberships: list[tuple] | None = None) -> None:
    """Create a minimal OpenWebUI-style SQLite DB at *path* (overwriting any existing one)."""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE user (id TEXT, email TEXT, name TEXT, role TEXT)")
    conn.execute("CREATE TABLE 'group' (id TEXT, name TEXT)")
    conn.execute("CREATE TABLE group_member (group_id TEXT, user_id TEXT)")
    if users:
        conn.executemany("INSERT INTO user VALUES (:id, :email, :name, :role)", users)
    if groups:
        conn.executemany("INSERT INTO 'group' VALUES (:id, :name)", groups)
    if memberships:
        conn.executemany("INSERT INTO group_member VALUES (?, ?)", memberships)
    conn.commit()
    conn.close()


def _mock_response(status_code: int = 200, json_data=None, ok: bool = True) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.ok = ok
    r.json.return_value = json_data if json_data is not None else {}
    r.text = str(json_data)
    r.raise_for_status = MagicMock()
    if not ok:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return r


@pytest.fixture()
def service(tmp_path, monkeypatch):
    """A UserSyncService pointed at a fresh temp DB, with valid config."""
    db = tmp_path / "webui.db"
    _make_db(str(db))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setenv("OPENWEBUI_ADMIN_API_KEY", "owui-admin-test")
    monkeypatch.setenv("OPENWEBUI_DB_PATH", str(db))
    import importlib
    import sync_users
    importlib.reload(sync_users)  # re-read env vars set above
    sync_users.OPENWEBUI_DB_PATH = str(db)
    svc = sync_users.UserSyncService()
    svc._db_path = str(db)
    return svc, sync_users, str(db)


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_exits_when_litellm_key_missing(self, tmp_path, monkeypatch):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
        monkeypatch.setenv("OPENWEBUI_ADMIN_API_KEY", "x")
        import importlib
        import sync_users
        importlib.reload(sync_users)
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", str(db)):
            with pytest.raises(SystemExit):
                sync_users.UserSyncService()

    def test_exits_when_admin_key_missing(self, tmp_path, monkeypatch):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
        monkeypatch.delenv("OPENWEBUI_ADMIN_API_KEY", raising=False)
        import importlib
        import sync_users
        importlib.reload(sync_users)
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", str(db)):
            with pytest.raises(SystemExit):
                sync_users.UserSyncService()

    def test_exits_when_db_missing(self, monkeypatch):
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
        monkeypatch.setenv("OPENWEBUI_ADMIN_API_KEY", "x")
        import importlib
        import sync_users
        importlib.reload(sync_users)
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", "/nonexistent/webui.db"):
            with pytest.raises(SystemExit):
                sync_users.UserSyncService()


# ---------------------------------------------------------------------------
# get_openwebui_users / get_openwebui_groups
# ---------------------------------------------------------------------------

class TestOpenWebUIReads:
    def test_get_users(self, service):
        svc, sync_users, db = service
        _make_db(db, users=[{"id": "u1", "email": "a@x.com", "name": "A", "role": "user"}])
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", db):
            users = svc.get_openwebui_users()
        assert len(users) == 1
        assert users[0]["email"] == "a@x.com"

    def test_get_groups_with_members(self, service):
        svc, sync_users, db = service
        _make_db(
            db,
            users=[{"id": "u1", "email": "a@x.com", "name": "A", "role": "user"}],
            groups=[{"id": "g1", "name": "Engineering"}],
            memberships=[("g1", "u1")],
        )
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", db):
            groups = svc.get_openwebui_groups()
        assert len(groups) == 1
        assert groups[0]["name"] == "Engineering"
        assert groups[0]["user_ids"] == ["u1"]

    def test_get_groups_empty_db_returns_empty_list(self, service):
        svc, sync_users, db = service
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", db):
            assert svc.get_openwebui_groups() == []


# ---------------------------------------------------------------------------
# create_litellm_user — no individual budget
# ---------------------------------------------------------------------------

class TestCreateLitellmUser:
    def test_payload_has_no_budget_field(self, service):
        svc, sync_users, db = service
        with patch.object(svc.session, "post", return_value=_mock_response(json_data={"user_id": "u1"})) as mock_post:
            svc.create_litellm_user({"id": "u1", "email": "a@x.com", "name": "A"})
        payload = mock_post.call_args.kwargs["json"]
        assert "max_budget" not in payload
        assert payload["auto_create_key"] is False

    def test_returns_none_on_http_error(self, service):
        svc, sync_users, db = service
        import requests
        with patch.object(svc.session, "post", side_effect=requests.exceptions.RequestException("boom")):
            result = svc.create_litellm_user({"id": "u1", "email": "a@x.com", "name": "A"})
        assert result is None


# ---------------------------------------------------------------------------
# ensure_team_provisioned / rename_openwebui_connection
# ---------------------------------------------------------------------------

class TestTeamProvisioning:
    def test_ensure_team_provisioned_skips_if_connection_exists(self, service):
        svc, sync_users, db = service
        existing_config = {
            "OPENAI_API_BASE_URLS": ["http://litellm:4000/v1"],
            "OPENAI_API_KEYS": ["sk-existing"],
            "OPENAI_API_CONFIGS": {"0": {"enable": True, "prefix_id": "Engineering"}},
        }
        with patch.object(svc, "get_openwebui_openai_config", return_value=existing_config):
            with patch.object(svc, "generate_team_key") as mock_gen:
                result = svc.ensure_team_provisioned({"id": "g1", "name": "Engineering"}, "team-1")
        assert result is False
        mock_gen.assert_not_called()

    def test_ensure_team_provisioned_creates_connection_and_sets_access(self, service):
        svc, sync_users, db = service
        empty_config = {"OPENAI_API_BASE_URLS": [], "OPENAI_API_KEYS": [], "OPENAI_API_CONFIGS": {}}
        with patch.object(svc, "get_openwebui_openai_config", return_value=empty_config), \
             patch.object(svc, "generate_team_key", return_value="sk-new-key"), \
             patch.object(svc, "update_openwebui_openai_config", return_value=True) as mock_update, \
             patch.object(svc, "sync_model_access") as mock_sync_access:
            result = svc.ensure_team_provisioned({"id": "g1", "name": "Engineering"}, "team-1")
        assert result is True
        base_urls, keys, api_configs = mock_update.call_args.args
        assert keys == ["sk-new-key"]
        assert api_configs["0"]["prefix_id"] == "Engineering"
        mock_sync_access.assert_called_once()

    def test_rename_connection_returns_none_when_no_match(self, service):
        svc, sync_users, db = service
        config = {"OPENAI_API_BASE_URLS": [], "OPENAI_API_KEYS": [], "OPENAI_API_CONFIGS": {}}
        with patch.object(svc, "get_openwebui_openai_config", return_value=config):
            result = svc.rename_openwebui_connection("Old", "New")
        assert result is None

    def test_rename_connection_updates_prefix_and_returns_key(self, service):
        svc, sync_users, db = service
        config = {
            "OPENAI_API_BASE_URLS": ["http://litellm:4000/v1"],
            "OPENAI_API_KEYS": ["sk-abc"],
            "OPENAI_API_CONFIGS": {"0": {"enable": True, "prefix_id": "Old"}},
        }
        with patch.object(svc, "get_openwebui_openai_config", return_value=config), \
             patch.object(svc, "update_openwebui_openai_config", return_value=True):
            key = svc.rename_openwebui_connection("Old", "New")
        assert key == "sk-abc"


# ---------------------------------------------------------------------------
# sync_model_access
# ---------------------------------------------------------------------------

class TestSyncModelAccess:
    def test_sets_access_grants_on_each_model(self, service):
        svc, sync_users, db = service
        models_response = _mock_response(json_data={"data": [{"id": "claude-haiku-4-5"}, {"id": "claude-sonnet-4-6"}]})
        with patch("requests.get", return_value=models_response), \
             patch.object(svc.owui_session, "post", return_value=_mock_response()) as mock_post:
            svc.sync_model_access({"id": "g1", "name": "Engineering"}, "sk-team-key")
        assert mock_post.call_count == 2
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        assert first_payload["id"] == "Engineering.claude-haiku-4-5"
        assert first_payload["access_grants"][0]["principal_id"] == "g1"

    def test_no_calls_when_model_list_fails(self, service):
        svc, sync_users, db = service
        import requests
        with patch("requests.get", side_effect=requests.exceptions.RequestException("down")), \
             patch.object(svc.owui_session, "post") as mock_post:
            svc.sync_model_access({"id": "g1", "name": "Engineering"}, "sk-team-key")
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# sync_groups — orchestration
# ---------------------------------------------------------------------------

class TestSyncGroups:
    def test_creates_team_for_new_group(self, service):
        svc, sync_users, db = service
        _make_db(db, groups=[{"id": "g1", "name": "Engineering"}])
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", db), \
             patch.object(svc, "get_litellm_teams", return_value=[]), \
             patch.object(svc, "create_litellm_team", return_value="team-1") as mock_create, \
             patch.object(svc, "ensure_team_provisioned", return_value=True):
            svc.sync_groups()
        mock_create.assert_called_once()

    def test_renames_team_when_group_renamed(self, service):
        svc, sync_users, db = service
        _make_db(db, groups=[{"id": "g1", "name": "New Name"}])
        existing_team = {"team_id": "team-1", "team_alias": "Old Name", "metadata": {"openwebui_group_id": "g1"}}
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", db), \
             patch.object(svc, "get_litellm_teams", return_value=[existing_team]), \
             patch.object(svc, "update_litellm_team_alias") as mock_rename, \
             patch.object(svc, "rename_openwebui_connection", return_value="sk-existing") as mock_rename_conn, \
             patch.object(svc, "sync_model_access") as mock_sync_access:
            svc.sync_groups()
        mock_rename.assert_called_once_with("team-1", "New Name")
        mock_rename_conn.assert_called_once_with("Old Name", "New Name")
        mock_sync_access.assert_called_once()

    def test_no_groups_logs_warning_and_returns(self, service, caplog):
        svc, sync_users, db = service
        with patch.object(sync_users, "OPENWEBUI_DB_PATH", db), \
             patch.object(svc, "create_litellm_team") as mock_create:
            svc.sync_groups()
        mock_create.assert_not_called()
