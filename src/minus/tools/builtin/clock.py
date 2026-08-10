"""Time-related tools."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from minus.config import Settings
from minus.tools.registry import registry

_DEFAULT_TIMEZONE = Settings.model_fields["timezone"].default


@registry.tool
def get_current_time(timezone: str = _DEFAULT_TIMEZONE) -> dict:
    """Get the current date and time.

    Args:
        timezone: IANA timezone name, e.g. "America/Chicago". Defaults to the
            assistant's configured local timezone.
    """
    now = datetime.now(ZoneInfo(timezone))
    return {
        "timezone": timezone,
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "iso": now.isoformat(timespec="seconds"),
    }
