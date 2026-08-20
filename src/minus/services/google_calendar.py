"""The Google Calendar REST API.

The same bargain as `google_tasks.py`, over the same grant: HTTP here, meaning
in `tools/google_calendar.py`.

Two API details are load-bearing and worth stating once rather than
rediscovering at each call site:

  * `singleEvents=true` on a listing expands a recurring event into the
    individual occurrences the user actually has. Without it a weekly standup
    is one row with a recurrence rule, which is not what "what's on Tuesday"
    means, and its id cannot be edited or deleted per-occurrence.
  * An all-day event's `end.date` is *exclusive*. A one-day event on the 21st
    ends on the 22nd. Google stores it that way and the tools layer converts,
    because no user and no model means "ends the 22nd" by "all day on the
    21st".
"""

from __future__ import annotations

import logging
from typing import Any

from minus.services.google import GoogleCredentials

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarClient:
    """One Google account's calendars, over HTTP."""

    def __init__(self, credentials: GoogleCredentials) -> None:
        self.credentials = credentials

    def request(self, method: str, path: str, **kwargs: Any) -> dict:
        return self.credentials.request(method, f"{API_ROOT}{path}", **kwargs)

    # ---- Calendars ----

    def list_calendars(self) -> list[dict]:
        """Every calendar on the account, with the primary one first.

        Google returns the list in no documented order, so the primary is
        lifted to the front here: it is the one an account that has never made
        a second calendar has, and the one "my calendar" means.
        """
        payload = self.request("GET", "/users/me/calendarList", params={"maxResults": 250})
        items = list(payload.get("items") or [])
        return sorted(items, key=lambda calendar: not calendar.get("primary", False))

    # ---- Events ----

    def list_events(
        self,
        calendar_id: str,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 100,
    ) -> list[dict]:
        payload = self.request(
            "GET",
            f"/calendars/{_quote(calendar_id)}/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": max_results,
                "singleEvents": True,
                "orderBy": "startTime",
            },
        )
        return list(payload.get("items") or [])

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        return self.request("POST", f"/calendars/{_quote(calendar_id)}/events", json=body)

    def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return self.request(
            "PATCH", f"/calendars/{_quote(calendar_id)}/events/{event_id}", json=body
        )

    def move_event(self, calendar_id: str, event_id: str, destination: str) -> dict:
        return self.request(
            "POST",
            f"/calendars/{_quote(calendar_id)}/events/{event_id}/move",
            params={"destination": destination},
        )

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.request("DELETE", f"/calendars/{_quote(calendar_id)}/events/{event_id}")


def _quote(calendar_id: str) -> str:
    """A calendar id safe in a path segment.

    Calendar ids are email addresses, and a secondary calendar's looks like
    `...@group.calendar.google.com`. The `@` is legal in a path segment and
    passes through, but an id is user data reaching a URL, so it is escaped
    rather than trusted to be well-behaved.
    """
    from urllib.parse import quote

    return quote(calendar_id, safe="")
