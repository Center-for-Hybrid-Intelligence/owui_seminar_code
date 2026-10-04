FROM python:3.11-slim

LABEL maintainer="Maceo"
LABEL description="OpenWebUI to LiteLLM User Sync Service"

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY sync_users.py .

# Make script executable
RUN chmod +x sync_users.py

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/health', timeout=5)" || exit 1

# Run the service
CMD ["python", "-u", "sync_users.py"]
