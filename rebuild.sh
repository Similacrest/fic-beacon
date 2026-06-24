#!/usr/bin/env bash
set -e
VERSION=$(git describe --tags 2>/dev/null || echo "dev")
export VERSION
echo "Building fic-beacon $VERSION…"
docker-compose up --build -d
echo "Done — running $VERSION"
