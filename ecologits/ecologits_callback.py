from litellm.integrations.custom_logger import CustomLogger
from ecologits.model_repository import models
from ecologits.impacts.llm import compute_llm_impacts
from datetime import datetime
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
    # Not handled by docker-entrypoint-initdb.d: that only fires on first Postgres init.
    # Wrapped in a session advisory lock: with 2+ gunicorn workers importing this module
    # at the same time, two concurrent "CREATE TABLE IF NOT EXISTS" can both try to create
    # the pg_type row for the table and collide on pg_class_relname_nsp_index (harmless but
    # noisy). The lock serializes the workers so the second one's IF NOT EXISTS is a no-op.
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(hashtext('EcoLogits_Impacts'))")
        try:
            cur.execute(
                'CREATE TABLE IF NOT EXISTS "EcoLogits_Impacts" ('
                'id SERIAL PRIMARY KEY, user_email VARCHAR(255), chat_id VARCHAR(255), '
                'model VARCHAR(255), tokens INTEGER, gwp_g FLOAT, energy_kwh FLOAT, '
                'timestamp TIMESTAMP DEFAULT NOW())'
            )
            conn.commit()
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext('EcoLogits_Impacts'))")
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[EcoLogits] ensure_table error: {e}")


ensure_table()


def get_provider_and_model(model):
    parts = model.split("/")
    if len(parts) > 1:
        return parts[0], parts[-1]
    model_name = parts[0]
    if "claude" in model_name:
        return "anthropic", model_name
    elif "gpt" in model_name or "o1" in model_name or "o3" in model_name or "o4" in model_name:
        return "openai", model_name
    elif "mistral" in model_name or "ministral" in model_name:
        return "mistralai", model_name
    elif "gemini" in model_name:
        return "google_genai", model_name
    return "openai", model_name


def compute_impacts_for_model(model, output_tokens):
    provider, model_name = get_provider_and_model(model)
    matched = models.find_model(provider=provider, model_name=model_name)
    if matched:
        params = matched.architecture.parameters
        if hasattr(params, "active") and hasattr(params, "total"):
            # MoE architecture: separate active/total parameter counts
            active_params = params.active
            total_params = params.total
        else:
            # Dense architecture: parameters is the count itself (all params active)
            active_params = params
            total_params = params
    else:
        active_params = 7
        total_params = 7
    result = compute_llm_impacts(
        model_active_parameter_count=active_params,
        model_total_parameter_count=total_params,
        output_token_count=output_tokens,
        if_electricity_mix_adpe=7.37e-8,
        if_electricity_mix_pe=9.0,
        if_electricity_mix_gwp=0.429,
        if_electricity_mix_wue=0.0,
        datacenter_pue=1.4,
        datacenter_wue=0.0,
    )
    return result, matched is not None


class ImpactHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/impacts/latest"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            user_email = params.get("user", [None])[0]
            chat_id = params.get("chat_id", [None])[0]

            try:
                conn = get_db_connection()
                cur = conn.cursor()
                if chat_id:
                    cur.execute(
                        'SELECT model, tokens, gwp_g, energy_kwh, timestamp FROM "EcoLogits_Impacts" '
                        'WHERE chat_id = %s ORDER BY timestamp DESC LIMIT 1',
                        (chat_id,)
                    )
                elif user_email:
                    cur.execute(
                        'SELECT model, tokens, gwp_g, energy_kwh, timestamp FROM "EcoLogits_Impacts" '
                        'WHERE user_email = %s ORDER BY timestamp DESC LIMIT 1',
                        (user_email,)
                    )
                else:
                    cur.execute(
                        'SELECT model, tokens, gwp_g, energy_kwh, timestamp FROM "EcoLogits_Impacts" '
                        'ORDER BY timestamp DESC LIMIT 1'
                    )
                row = cur.fetchone()

                session_total_gwp_g = None
                session_total_energy_kwh = None
                if chat_id:
                    cur.execute(
                        'SELECT SUM(gwp_g), SUM(energy_kwh) FROM "EcoLogits_Impacts" WHERE chat_id = %s',
                        (chat_id,)
                    )
                    sum_row = cur.fetchone()
                    session_total_gwp_g = float(sum_row[0] or 0.0)
                    session_total_energy_kwh = float(sum_row[1] or 0.0)

                cur.close()
                conn.close()

                if row:
                    entry = {
                        "model": row[0],
                        "tokens": row[1],
                        "gwp_g": row[2],
                        "energy_kwh": row[3],
                        "timestamp": row[4].isoformat(),
                        "session_total_gwp_g": session_total_gwp_g,
                        "session_total_energy_kwh": session_total_energy_kwh,
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(entry).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception as e:
                print(f"[EcoLogits] DB read error: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_impact_server():
    server = HTTPServer(("0.0.0.0", 4001), ImpactHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("[EcoLogits] Impact server running on port 4001")


if os.getenv("IMPACT_SERVER_STARTED") != "1":
    try:
        start_impact_server()
        os.environ["IMPACT_SERVER_STARTED"] = "1"
    except OSError:
        print("[EcoLogits] Impact server already running, skipping")


class EcoLogitsCallback(CustomLogger):

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            model = kwargs.get("model", "unknown")
            usage = getattr(response_obj, "usage", None)
            if usage is None:
                return
            output_tokens = getattr(usage, "completion_tokens", 0)
            if output_tokens == 0:
                return

            # Get user email from request headers
            litellm_params = kwargs.get("litellm_params", {})
            metadata = litellm_params.get("metadata", {})
            headers = metadata.get("headers", {})
            user_email = headers.get("x-openwebui-user-email", None)
            chat_id = headers.get("x-openwebui-chat-id", None)

            impacts, found = compute_impacts_for_model(model, output_tokens)

            gwp_val = impacts.gwp.value
            energy_val = impacts.energy.value

            if hasattr(gwp_val, 'min') and hasattr(gwp_val, 'max'):
                gwp_g = ((gwp_val.min + gwp_val.max) / 2) * 1000
                energy_kwh = (energy_val.min + energy_val.max) / 2
            else:
                gwp_g = float(gwp_val) * 1000
                energy_kwh = float(energy_val)

            # Write to PostgreSQL
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO "EcoLogits_Impacts" (user_email, chat_id, model, tokens, gwp_g, energy_kwh) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (user_email, chat_id, model, output_tokens, round(gwp_g, 4), round(energy_kwh, 6))
            )
            conn.commit()
            cur.close()
            conn.close()

            if not found:
                print(f"[EcoLogits] model not in repository, using fallback: {model}")
            print(f"[EcoLogits] user={user_email} model={model} | tokens={output_tokens} | ~{gwp_g:.4f} gCO2eq | {energy_kwh:.6f} kWh")

        except Exception as e:
            print(f"[EcoLogits] error: {e}")


proxy_handler_instance = EcoLogitsCallback()