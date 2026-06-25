"""Single source of truth for the app version: pyproject.toml's [project].version.

The version lives in exactly one place — pyproject.toml — and is read from there at
runtime. pyproject.toml is copied into the image (`COPY pyproject.toml .`), so this
resolves the same string in dev and in the container. No baked-in env var, no git
describe, no duplication.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


def _read_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        # Fallback to installed package metadata (may lag pyproject if not reinstalled).
        try:
            from importlib.metadata import PackageNotFoundError, version

            return version("fic-beacon")
        except PackageNotFoundError:
            return "dev"


__version__ = _read_version()
