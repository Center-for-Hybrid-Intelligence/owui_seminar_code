# Optional local build — the stack uses the published image by default:
#   ghcr.io/open-webui/mcpo:main
# See docker-compose.yml (mcpo service). Keep this file only if you need to
# customize the image; otherwise prefer pulling the official one.

FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Node is optional but improves HTML simplification for mcp-server-fetch.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN uv pip install --system mcpo && rm -rf /root/.cache

EXPOSE 8000
ENTRYPOINT ["mcpo"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--", "uvx", "--with", "mcp<2", "mcp-server-fetch"]
