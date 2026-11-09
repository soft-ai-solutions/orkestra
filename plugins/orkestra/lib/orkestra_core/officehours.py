"""Weekday commit and push window for the Orkestra checkout only.

Closed on Monday-Friday from 09:00 inclusive until 18:00 exclusive, in a fixed GMT+3 offset
(not the machine's local zone). Saturday and Sunday stay open all day. The clock is passed in
so tests never read the wall clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

GMT3 = timezone(timedelta(hours=3))
_OPEN_AT = (18, 0)   # first allowed minute on a weekday
_CLOSED_AT = (9, 0)

REASON = ("Orkestra refuses commit and push on weekdays from 09:00 to 18:00 GMT+3. "
          "Try again at 18:00 or later, or on Saturday or Sunday.")

_MARKER = Path("plugins") / "orkestra" / "lib" / "orkestra_core" / "guards.py"
_LAUNCHER = Path("plugins") / "orkestra" / "bin" / "orkestra"


def commit_push_closed(moment: datetime) -> bool:
    """True when `moment` falls inside the weekday window. Naive values are read as GMT+3."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=GMT3)
    local = moment.astimezone(GMT3)
    if local.weekday() >= 5:
        return False
    closed = local.replace(hour=_CLOSED_AT[0], minute=_CLOSED_AT[1], second=0, microsecond=0)
    opened = local.replace(hour=_OPEN_AT[0], minute=_OPEN_AT[1], second=0, microsecond=0)
    return closed <= local < opened


def in_orkestra_checkout(cwd: str) -> bool:
    """True when `cwd` is the Orkestra source tree or a directory inside it."""
    current = Path(cwd).resolve()
    for parent in (current, *current.parents):
        if (parent / _MARKER).is_file() and (parent / _LAUNCHER).is_file():
            return True
    return False
