#!/usr/bin/env bash
set -e
# Version is read at runtime from pyproject.toml (app/version.py) — nothing to inject.
VERSION=$(python -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])" 2>/dev/null || echo "dev")
echo "Building fic-beacon $VERSION…"
docker-compose up --build -d
echo "Done — running $VERSION"
