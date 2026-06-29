FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
  && rm -rf /var/lib/apt/lists/*

RUN pip install -U uv

COPY pyproject.toml .
RUN uv sync --no-dev

COPY app/ ./app/
# Alembic migrations run on startup (init_db → run_migrations); the scripts + config must
# ship in the image. Schema is migration-owned now — there is no create_all in production.
COPY alembic.ini .
COPY alembic/ ./alembic/

# pyproject.toml (copied above) is the single source of truth for the version,
# read at runtime by app/version.py — nothing to bake in here.

# Data volume will be mounted at /data (app SQLite DB)
VOLUME ["/data", "/calibre-library"]

EXPOSE 8000

# Single worker: APScheduler must not run in multiple processes
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
