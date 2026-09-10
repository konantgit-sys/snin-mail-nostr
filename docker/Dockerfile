# Nostr Mail Bridge — self-hosted deploy
# Build:  docker build -t nostr-mail-bridge .
# Run:    docker run -d -p 8123:8123 -v $PWD/config.json:/app/web/config.json:ro nostr-mail-bridge
FROM python:3.11-slim

WORKDIR /app

# system deps для secp256k1 (sdist build)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/

ENV PYTHONPATH=/app/src
WORKDIR /app/web

EXPOSE 8123

# healthcheck: GET /api/status
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8123/api/status || exit 1

CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8123"]
