FROM ghcr.io/berriai/litellm:main-stable

# Combined dependencies for the optional callbacks (cost-tracking/litellm_cost_callback.py,
# ecologits/ecologits_callback.py) — both mounted into this image at runtime, see
# docker-compose.yml. Neither callback actually runs unless referenced in the
# `callbacks:` list of litellm-config.yaml, so installing both here is always safe.
RUN /app/.venv/bin/python3 -m ensurepip --upgrade && \
    /app/.venv/bin/python3 -m pip install psycopg2-binary "ecologits[litellm]" --quiet --no-cache-dir
