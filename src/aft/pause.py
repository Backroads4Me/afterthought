"""Pausing background work (BUILD-PLAN F38).

A pause is state, not configuration: one marker file in the home directory saying since
when and why. While it stands, `aft run` and `aft dream` exit at once, whatever started
them (the timer, a session's end, or a person), and a session's end does not start the
work service. Capture is untouched: hooks still queue each session and still print the
memory block, so nothing is lost and `aft resume` picks the backlog up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from aft.config import Config

PAUSE_FILE = "paused"


@dataclass(frozen=True)
class Pause:
    since: str
    reason: str

    def line(self) -> str:
        """The pause as one line a command prints."""
        why = f": {self.reason}" if self.reason else ""
        return f"paused since {self.since}{why}; aft resume starts background work again"


def paused(config: Config) -> Pause | None:
    """The pause in force, or None. A marker that cannot be read still pauses: it was put
    there to stop work, and guessing otherwise would start it."""
    path = config.home / PAUSE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Pause(since=str(data["since"]), reason=str(data.get("reason", "")))
    except (OSError, ValueError, KeyError, TypeError):
        return Pause(since="an unknown time", reason=f"{path} could not be read")


def pause(config: Config, reason: str = "", now: datetime | None = None) -> tuple[Pause, bool]:
    """Pause background work. Returns the pause in force and whether this call made it; an
    existing pause is kept as it was, so its time and reason stay true."""
    existing = paused(config)
    if existing is not None:
        return existing, False
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    made = Pause(since=f"{moment:%Y-%m-%d %H:%M}Z", reason=reason.strip())
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / PAUSE_FILE).write_text(
        json.dumps({"since": made.since, "reason": made.reason}) + "\n", encoding="utf-8"
    )
    return made, True


def resume(config: Config) -> bool:
    """End the pause. Returns whether there was one."""
    path = config.home / PAUSE_FILE
    if not path.exists():
        return False
    path.unlink()
    return True
