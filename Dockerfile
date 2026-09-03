# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.34.0" \
    "jinja2>=3.1.0" \
    "python-multipart>=0.0.18" \
    "feedparser>=6.0.11" \
    "httpx>=0.28.0" \
    "apscheduler>=3.10.4" \
    "cryptography>=42.0.0" \
    "psycopg[binary]>=3.2"

COPY . .

# SQLite lives on a volume so data survives container rebuilds
VOLUME /app/data
ENV FEEDECHO_DB_PATH=/app/data/feedecho.db

# The application runs as uid 10001, never as root: it parses untrusted remote
# feed content all day. There is deliberately no USER directive — the
# entrypoint starts as root only long enough to hand an inherited root-owned
# /app/data (every deployment created before v1.13.6) to that user, then drops
# privileges with setpriv. Pass --user to skip both steps.
RUN useradd --create-home --uid 10001 --user-group feedecho \
    && mkdir -p /app/data \
    && chown -R feedecho:feedecho /app \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 8453

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8453"]
