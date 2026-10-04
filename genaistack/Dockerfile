# genaistack installer — drives the host Docker daemon via the socket.
# Build from the repo root: docker build -t genaistack-installer -f genaistack/Dockerfile .
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl openssl python3 \
      apt-transport-https gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY genaistack/entrypoint.sh /usr/local/bin/genaistack-entrypoint.sh
COPY genaistack/setup.sh /opt/genaistack/setup.sh
RUN chmod 755 /usr/local/bin/genaistack-entrypoint.sh /opt/genaistack/setup.sh

WORKDIR /opt/genaistack
ENTRYPOINT ["/usr/local/bin/genaistack-entrypoint.sh"]
