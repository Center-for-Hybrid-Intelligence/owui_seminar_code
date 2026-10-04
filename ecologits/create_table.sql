-- Reference schema for EcoLogits_Impacts — kept for manual ops/DR (psql direct),
-- not wired into docker-entrypoint-initdb.d (only fires on first Postgres init,
-- which already happened on every deployed instance). ecologits_callback.py
-- creates this table itself (CREATE TABLE IF NOT EXISTS) on every litellm start,
-- so this file stays in sync with that as the source of truth.
CREATE TABLE IF NOT EXISTS "EcoLogits_Impacts" (id SERIAL PRIMARY KEY, user_email VARCHAR(255), chat_id VARCHAR(255), model VARCHAR(255), tokens INTEGER, gwp_g FLOAT, energy_kwh FLOAT, timestamp TIMESTAMP DEFAULT NOW());
