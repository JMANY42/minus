"""The alarm tool: a thin wrapper over the `alarm` CLI.

The alarm itself lives in a separate always-on daemon, so it still rings
when MINUS is stopped. This only sets its time.

Constructed rather than registered at import, the way the Google tools are,
because the command comes from `Settings` (`MINUS_ALARM_COMMAND`) and
`assembly.py` is the only module allowed to read those. The default is the
full ~/.cargo/bin path rather than a bare `alarm`, because the systemd user
manager running `minus serve` does not have ~/.cargo/bin on its PATH.
"""

from __future__ import annotations

import subprocess
from typing import Any

from minus.errors import ToolExecutionError

# The heading this is filed under in the dashboard's tool list.
CATEGORY = "alarm"

# The CLI answers immediately; anything slower is a hung daemon, not a slow one.
_TIMEOUT_SECONDS = 10


class AlarmTools:
    """The alarm tools, bound to the command that drives the alarm daemon."""

    def __init__(self, command: str, *, timezone: str) -> None:
        self.command = command
        # The user's own IANA zone, from MINUS_TIMEZONE, for when they name a
        # time and no place. Required rather than defaulted here, so there is
        # one default and it lives in config.py.
        self.timezone = timezone

    def register(self, registry: Any) -> None:
        """Attach the tools to a registry, as the Google groups are attached."""
        for tool in (self.set_alarm, self.enable_alarm, self.disable_alarm, self.alarm_status, self.stop_alarm):
            registry.tool(tool, category=CATEGORY)

    # ---- Tools ----

    def set_alarm(self, time: str, timezone: str = "") -> dict:
        """Set the user's alarm clock.

        Args:
            time: 24-hour time in HH:MM:SS format, e.g. "07:30:00".
            timezone: IANA timezone name, e.g. "America/Chicago". Leave empty
                for the user's own timezone.
        """
        return self._run("set", time, timezone.strip() or self.timezone)

    def enable_alarm(self) -> dict:
        """Turn the user's alarm clock on, keeping the time already set."""
        return self._run("on")

    def disable_alarm(self) -> dict:
        """Disable the alarm from ringing, keeping the time already set.
            NOTE: If the alarm is already ringing, this will not stop it from ringing."""
        return self._run("off")

    def alarm_status(self) -> dict:
        """Get the time, timezone, and on/off state of the user's alarm clock."""
        return self._run("status")

    def stop_alarm(self) -> dict:
        """Stop an alarm that is currently playing."""
        return self._run("stop")

    # ---- Internals ----

    def _run(self, *args: str) -> dict:
        try:
            result = subprocess.run(
                [self.command, *args],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                f"The alarm command {self.command!r} was not found; "
                "tell the user to try setting MINUS_ALARM_COMMAND to its full path"
            ) from exc
        return {
            "ok": result.returncode == 0,
            "output": (result.stdout or result.stderr).strip(),
        }
