-- Reference schema for LiteLLM_Cost_Tracking — kept for manual ops/DR (psql direct),
-- not wired into docker-entrypoint-initdb.d (only fires on first Postgres init,
-- which already happened on every deployed instance). litellm_cost_callback.py
-- creates this table itself (CREATE TABLE IF NOT EXISTS) on every litellm start,
-- so this file stays in sync with that as the source of truth.
CREATE TABLE IF NOT EXISTS "LiteLLM_Cost_Tracking" (id SERIAL PRIMARY KEY, user_email VARCHAR(255), chat_id VARCHAR(255), model VARCHAR(255), input_tokens INTEGER, output_tokens INTEGER, cost_usd FLOAT, timestamp TIMESTAMP DEFAULT NOW());
ALTER TABLE "LiteLLM_Cost_Tracking" ADD COLUMN IF NOT EXISTS chat_id VARCHAR(255);
