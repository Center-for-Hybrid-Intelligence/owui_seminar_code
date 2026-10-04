from litellm.integrations.custom_logger import CustomLogger
import litellm
import threading
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_db_connection():
    url = DATABASE_URL
    # LiteLLM rewrites DATABASE_URL for Prisma with pool params psycopg2 rejects.
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    unsupported = {
        "connection_limit",
        "pool_timeout",
        "schema",
        "connect_timeout",
        "socket_timeout",
        "max_idle_connection_lifetime",
        "pgbouncer",
    }
    supported = {k: v for k, v in params.items() if k not in unsupported}
    clean_query = urlencode({k: v[0] for k, v in supported.items()})
    clean_url = urlunparse(parsed._replace(query=clean_query))
    return psycopg2.connect(clean_url)


def ensure_table():
    # Idempotent — runs on every litellm start (fresh volume or already-initialized one).
    # Not handled by docker-entrypoint-initdb.d: that only fires on first Postgres init,
    # which already happened on every deployed instance before this callback existed.
    # Wrapped in a session advisory lock: with 2+ gunicorn workers importing this module
    # at the same time, two concurrent "CREATE TABLE IF NOT EXISTS" can both try to create
    # the pg_type row for the table and collide on pg_class_relname_nsp_index (harmless but
    # noisy). The lock serializes the workers so the second one's IF NOT EXISTS is a no-op.
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(hashtext('LiteLLM_Cost_Tracking'))")
        try:
            cur.execute(
                'CREATE TABLE IF NOT EXISTS "LiteLLM_Cost_Tracking" ('
                'id SERIAL PRIMARY KEY, user_email VARCHAR(255), chat_id VARCHAR(255), '
                'model VARCHAR(255), input_tokens INTEGER, output_tokens INTEGER, '
                'cost_usd FLOAT, timestamp TIMESTAMP DEFAULT NOW())'
            )
            cur.execute('ALTER TABLE "LiteLLM_Cost_Tracking" ADD COLUMN IF NOT EXISTS chat_id VARCHAR(255)')
            conn.commit()
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext('LiteLLM_Cost_Tracking'))")
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[CostTracking] ensure_table error: {e}")


ensure_table()


class CostHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/cost/latest"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            user_email = params.get("user", [None])[0]
            chat_id = params.get("chat_id", [None])[0]

            try:
                conn = get_db_connection()
                cur = conn.cursor()
                if chat_id:
                    cur.execute(
                        'SELECT model, input_tokens, output_tokens, cost_usd, timestamp FROM "LiteLLM_Cost_Tracking" '
                        'WHERE chat_id = %s ORDER BY timestamp DESC LIMIT 1',
                        (chat_id,)
                    )
                elif user_email:
                    cur.execute(
                        'SELECT model, input_tokens, output_tokens, cost_usd, timestamp FROM "LiteLLM_Cost_Tracking" '
                        'WHERE user_email = %s ORDER BY timestamp DESC LIMIT 1',
                        (user_email,)
                    )
                else:
                    cur.execute(
                        'SELECT model, input_tokens, output_tokens, cost_usd, timestamp FROM "LiteLLM_Cost_Tracking" '
                        'ORDER BY timestamp DESC LIMIT 1'
                    )
                row = cur.fetchone()

                session_total_cost = None
                if chat_id:
                    cur.execute(
                        'SELECT SUM(cost_usd) FROM "LiteLLM_Cost_Tracking" WHERE chat_id = %s',
                        (chat_id,)
                    )
                    session_total_cost = float(cur.fetchone()[0] or 0.0)

                cur.close()
                conn.close()

                if row:
                    entry = {
                        "model": row[0],
                        "input_tokens": row[1],
                        "output_tokens": row[2],
                        "cost_usd": row[3],
                        "timestamp": row[4].isoformat(),
                        "session_total_cost": session_total_cost,
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(entry).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception as e:
                print(f"[CostTracking] DB read error: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_cost_server():
    server = HTTPServer(("0.0.0.0", 4002), CostHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("[CostTracking] Cost server running on port 4002")


if os.getenv("COST_SERVER_STARTED") != "1":
    try:
        start_cost_server()
        os.environ["COST_SERVER_STARTED"] = "1"
    except OSError:
        print("[CostTracking] Cost server already running, skipping")


class CostTrackingCallback(CustomLogger):

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            model = kwargs.get("model", "unknown")
            usage = getattr(response_obj, "usage", None)
            if usage is None:
                return
            input_tokens = getattr(usage, "prompt_tokens", 0)
            output_tokens = getattr(usage, "completion_tokens", 0)
            if input_tokens == 0 and output_tokens == 0:
                return

            # Get user email from request headers
            litellm_params = kwargs.get("litellm_params", {})
            metadata = litellm_params.get("metadata", {})
            headers = metadata.get("headers", {})
            user_email = headers.get("x-openwebui-user-email", None)
            chat_id = headers.get("x-openwebui-chat-id", None)

            # LiteLLM calcule déjà le coût (input+output) si le modèle est connu dans sa base de pricing
            cost_usd = kwargs.get("response_cost")
            if cost_usd is None:
                cost_usd = litellm.completion_cost(completion_response=response_obj, model=model)
            cost_usd = float(cost_usd or 0.0)

            # Write to PostgreSQL
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO "LiteLLM_Cost_Tracking" (user_email, chat_id, model, input_tokens, output_tokens, cost_usd) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (user_email, chat_id, model, input_tokens, output_tokens, round(cost_usd, 6))
            )
            conn.commit()
            cur.close()
            conn.close()

            print(f"[CostTracking] user={user_email} model={model} | in={input_tokens} out={output_tokens} | ${cost_usd:.6f}")

        except Exception as e:
            print(f"[CostTracking] error: {e}")


proxy_handler_instance = CostTrackingCallback()
