"""What the running release cycle / feed check is doing right now, for the dashboard banner.

A plain module-level string: the cycle runs in one scheduler thread under `scheduler._cycle_lock`
and the dashboard only reads it, so a stale or racy read just shows the previous stage for a poll.
"""
from __future__ import annotations

_stage = ""


def set_stage(text: str) -> None:
    global _stage
    _stage = text


def get_stage() -> str:
    return _stage


def clear() -> None:
    set_stage("")
