"""
Tests for revoke.py — OAuth revocation script.
Run with: pytest test_revoke.py -v
"""

import os
import sqlite3
import sys
import tempfile
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path: str, users: list[dict] | None = None) -> None:
    """Create a minimal OpenWebUI-style SQLite DB at *path*."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE user (id TEXT, email TEXT, name TEXT)")
    if users:
        conn.executemany(
            "INSERT INTO user VALUES (:id, :email, :name)", users
        )
    conn.commit()
    conn.close()


def _mock_response(status_code: int = 200, json_data: dict | None = None, ok: bool = True) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.ok = ok
    r.json.return_value = json_data or {}
    r.text = str(json_data)
    return r


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_exits_when_env_vars_missing(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        with patch.dict(os.environ, {
            "MICROSOFT_CLIENT_ID": "",
            "MICROSOFT_CLIENT_SECRET": "",
            "MICROSOFT_CLIENT_TENANT_ID": "",
            "LITELLM_MASTER_KEY": "",
            "OPENWEBUI_DB_PATH": str(db),
        }, clear=False):
            import revoke
            with pytest.raises(SystemExit):
                revoke.validate_config()

    def test_exits_when_db_missing(self):
        with patch.dict(os.environ, {
            "MICROSOFT_CLIENT_ID": "cid",
            "MICROSOFT_CLIENT_SECRET": "csecret",
            "MICROSOFT_CLIENT_TENANT_ID": "tid",
            "LITELLM_MASTER_KEY": "sk-test",
        }, clear=False):
            import revoke
            with patch.object(revoke, "OPENWEBUI_DB_PATH", "/nonexistent/path/webui.db"):
                with pytest.raises(SystemExit):
                    revoke.validate_config()

    def test_passes_when_all_valid(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        with patch.dict(os.environ, {
            "MICROSOFT_CLIENT_ID": "cid",
            "MICROSOFT_CLIENT_SECRET": "csecret",
            "MICROSOFT_CLIENT_TENANT_ID": "tid",
            "LITELLM_MASTER_KEY": "sk-test",
            "OPENWEBUI_DB_PATH": str(db),
        }, clear=False):
            import revoke
            revoke.validate_config()  # should not raise


# ---------------------------------------------------------------------------
# get_owui_users_by_email
# ---------------------------------------------------------------------------

class TestGetOwuiUsers:
    def test_returns_users_keyed_by_email(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db), [
            {"id": "u1", "email": "Alice@example.com", "name": "Alice"},
            {"id": "u2", "email": "bob@example.com", "name": "Bob"},
        ])
        import revoke
        with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
            result = revoke.get_owui_users_by_email()
        assert "alice@example.com" in result
        assert "bob@example.com" in result
        assert result["alice@example.com"]["id"] == "u1"

    def test_empty_table_returns_empty_dict(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        import revoke
        with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
            result = revoke.get_owui_users_by_email()
        assert result == {}

    def test_ignores_null_email(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db), [
            {"id": "u1", "email": None, "name": "NoEmail"},
            {"id": "u2", "email": "valid@example.com", "name": "Valid"},
        ])
        import revoke
        with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
            result = revoke.get_owui_users_by_email()
        assert len(result) == 1
        assert "valid@example.com" in result

    def test_returns_empty_dict_on_sqlite_error(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        import revoke
        with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
            with patch("sqlite3.connect") as mock_connect:
                mock_conn = MagicMock()
                mock_conn.__bool__ = lambda self: True
                mock_conn.cursor.return_value.execute.side_effect = sqlite3.Error("locked")
                mock_connect.return_value = mock_conn
                result = revoke.get_owui_users_by_email()
        assert result == {}

    def test_conn_closed_on_success(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        import revoke
        with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
            with patch("sqlite3.connect") as mock_connect:
                mock_conn = MagicMock()
                mock_conn.__bool__ = lambda self: True
                mock_conn.cursor.return_value.fetchall.return_value = []
                mock_connect.return_value = mock_conn
                revoke.get_owui_users_by_email()
        mock_conn.close.assert_called_once()

    def test_conn_closed_on_sqlite_error(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_db(str(db))
        import revoke
        with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
            with patch("sqlite3.connect") as mock_connect:
                mock_conn = MagicMock()
                mock_conn.__bool__ = lambda self: True
                mock_conn.cursor.return_value.execute.side_effect = sqlite3.Error("locked")
                mock_connect.return_value = mock_conn
                revoke.get_owui_users_by_email()
        mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# get_disabled_azure_users
# ---------------------------------------------------------------------------

class TestGetDisabledAzureUsers:
    def test_returns_users_keyed_by_email(self):
        import revoke
        page = {
            "value": [
                {"id": "az1", "mail": "Disabled@corp.com", "displayName": "D User"},
            ]
        }
        with patch("requests.get", return_value=_mock_response(json_data=page)):
            result = revoke.get_disabled_azure_users("fake-token")
        assert "disabled@corp.com" in result
        assert result["disabled@corp.com"]["id"] == "az1"

    def test_empty_response(self):
        import revoke
        with patch("requests.get", return_value=_mock_response(json_data={"value": []})):
            result = revoke.get_disabled_azure_users("fake-token")
        assert result == {}

    def test_ignores_users_without_email(self):
        import revoke
        page = {
            "value": [
                {"id": "az1", "mail": None, "displayName": "No Email"},
                {"id": "az2", "mail": "valid@corp.com", "displayName": "Valid"},
            ]
        }
        with patch("requests.get", return_value=_mock_response(json_data=page)):
            result = revoke.get_disabled_azure_users("fake-token")
        assert len(result) == 1
        assert "valid@corp.com" in result

    def test_follows_pagination(self):
        import revoke
        page1 = {
            "value": [{"id": "az1", "mail": "user1@corp.com", "displayName": "U1"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc",
        }
        page2 = {
            "value": [{"id": "az2", "mail": "user2@corp.com", "displayName": "U2"}],
        }
        responses = [_mock_response(json_data=page1), _mock_response(json_data=page2)]
        with patch("requests.get", side_effect=responses):
            result = revoke.get_disabled_azure_users("fake-token")
        assert "user1@corp.com" in result
        assert "user2@corp.com" in result


# ---------------------------------------------------------------------------
# revoke_azure_sessions
# ---------------------------------------------------------------------------

class TestRevokeAzureSessions:
    def test_dry_run_makes_no_http_call(self):
        import revoke
        with patch.object(revoke, "DRY_RUN", True):
            with patch("requests.post") as mock_post:
                revoke.revoke_azure_sessions("token", "azure-id", "user@corp.com")
        mock_post.assert_not_called()

    def test_posts_to_correct_url(self):
        import revoke
        mock_r = _mock_response(ok=True)
        with patch.object(revoke, "DRY_RUN", False):
            with patch("requests.post", return_value=mock_r) as mock_post:
                revoke.revoke_azure_sessions("my-token", "azure-123", "user@corp.com")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "azure-123/revokeSignInSessions" in args[0]
        assert kwargs["headers"]["Authorization"] == "Bearer my-token"

    def test_logs_error_on_http_failure(self):
        import revoke
        mock_r = _mock_response(ok=False, status_code=403)
        mock_r.text = "Forbidden"
        with patch.object(revoke, "DRY_RUN", False):
            with patch("requests.post", return_value=mock_r):
                # Should not raise — just logs error
                revoke.revoke_azure_sessions("token", "azure-id", "user@corp.com")


# ---------------------------------------------------------------------------
# delete_litellm_user
# ---------------------------------------------------------------------------

class TestDeleteLitellmUser:
    def test_dry_run_makes_no_http_call(self):
        import revoke
        with patch.object(revoke, "DRY_RUN", True):
            with patch("requests.delete") as mock_delete:
                revoke.delete_litellm_user("owui-123", "user@corp.com")
        mock_delete.assert_not_called()

    def test_deletes_correct_user(self):
        import revoke
        mock_r = _mock_response(ok=True)
        with patch.object(revoke, "DRY_RUN", False):
            with patch("requests.delete", return_value=mock_r) as mock_delete:
                revoke.delete_litellm_user("owui-123", "user@corp.com")
        mock_delete.assert_called_once()
        _, kwargs = mock_delete.call_args
        assert kwargs["json"] == {"user_ids": ["owui-123"]}

    def test_logs_error_on_http_failure(self):
        import revoke
        mock_r = _mock_response(ok=False, status_code=500)
        mock_r.text = "Internal Server Error"
        with patch.object(revoke, "DRY_RUN", False):
            with patch("requests.delete", return_value=mock_r):
                revoke.delete_litellm_user("owui-123", "user@corp.com")


# ---------------------------------------------------------------------------
# main (integration)
# ---------------------------------------------------------------------------

class TestMain:
    def _base_patches(self, tmp_path, users_in_owui, disabled_azure):
        db = tmp_path / "webui.db"
        _make_db(str(db), users_in_owui)
        patches = {
            "revoke.validate_config": patch("revoke.validate_config"),
            "revoke.get_graph_token": patch("revoke.get_graph_token", return_value="fake-token"),
            "revoke.get_disabled_azure_users": patch("revoke.get_disabled_azure_users", return_value=disabled_azure),
            "revoke.OPENWEBUI_DB_PATH": patch.object(__import__("revoke"), "OPENWEBUI_DB_PATH", str(db)),
        }
        return patches

    def test_nothing_to_do_when_no_match(self, tmp_path):
        import revoke
        with patch("revoke.validate_config"), \
             patch("revoke.get_graph_token", return_value="token"), \
             patch("revoke.get_disabled_azure_users", return_value={"gone@corp.com": {"id": "az1"}}), \
             patch.object(revoke, "OPENWEBUI_DB_PATH", str(tmp_path / "webui.db")), \
             patch("revoke.revoke_azure_sessions") as mock_revoke, \
             patch("revoke.delete_litellm_user") as mock_delete:
            db = tmp_path / "webui.db"
            _make_db(str(db), [{"id": "u1", "email": "other@corp.com", "name": "Other"}])
            with patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)):
                revoke.main()
        mock_revoke.assert_not_called()
        mock_delete.assert_not_called()

    def test_revokes_and_deletes_matching_user(self, tmp_path):
        import revoke
        db = tmp_path / "webui.db"
        _make_db(str(db), [{"id": "owui-u1", "email": "departed@corp.com", "name": "Departed"}])
        disabled = {"departed@corp.com": {"id": "az-u1", "mail": "departed@corp.com"}}

        with patch("revoke.validate_config"), \
             patch("revoke.get_graph_token", return_value="token"), \
             patch("revoke.get_disabled_azure_users", return_value=disabled), \
             patch.object(revoke, "OPENWEBUI_DB_PATH", str(db)), \
             patch.object(revoke, "DRY_RUN", False), \
             patch("revoke.revoke_azure_sessions") as mock_revoke, \
             patch("revoke.delete_litellm_user") as mock_delete:
            revoke.main()

        mock_revoke.assert_called_once_with("token", "az-u1", "departed@corp.com")
        mock_delete.assert_called_once_with("owui-u1", "departed@corp.com")
