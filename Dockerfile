# ---- Builder ----
FROM python:3.12.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && \
    curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | bash && apt-get update && apt-get install -y infisical=0.43.17 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock /app/
RUN uv sync --no-dev --no-cache --frozen

# Copy source code
COPY . /app/

# ---- Runtime ----
FROM python:3.12.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /app /app
COPY --from=builder /usr/bin/infisical /usr/bin/infisical
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]

CMD ["uv", "run", "--no-dev", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]