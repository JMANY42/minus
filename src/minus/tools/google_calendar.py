"""The Google Calendar tools: title, when, where, which calendar.

The same five verbs as the task tools and the same rule behind them -- never
invent a field the user is entitled to have chosen -- but the "when" of an
event is a harder thing to be sure about than a due date, and most of what is
specific to this module is about that.

An event is either all-day or it runs between two times, and a model that has
been told "put the dentist in on Tuesday" knows neither. So `add_google_event`
asks for both halves and then checks them against each other: `all_day` is a
required argument, `start` and `end` are required arguments, and a call whose
flag and whose values disagree -- all-day with a time attached, or timed with
no time given -- is refused as a question rather than resolved by preferring
one over the other. That cross-check is the point. Either half alone can be
filled in plausibly by a model that is guessing; the two of them agreeing takes
information that only comes from the user.

Two API conventions are handled here so that nothing above has to know them:
Google's all-day `end.date` is *exclusive* (a one-day event on the 21st ends on
the 22nd), and a timed event's `dateTime` is paired with an explicit `timeZone`
rather than an offset. Users say "all day Tuesday" and mean Tuesday, so the
conversion happens at this boundary, in both directions.

Calendars are filtered to the ones that can actually be written to before the
user is asked to choose between them. Most accounts carry subscribed read-only
calendars -- holidays, birthdays, someone else's shared work calendar -- and
offering those as somewhere to put a dentist appointment would turn one real
choice into a menu of five, four of which fail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from minus.errors import ClarificationNeeded, GoogleError, ToolArgumentError
from minus.services.google_calendar import GoogleCalendarClient
from minus.tools.google_shared import (
    When,
    choose_container,
    locate,
    match_one,
    parse_when,
    require,
    said_none,
    today_in,
)

logger = logging.getLogger(__name__)

SETTING = "MINUS_GOOGLE_CALENDAR"

# Calendars an event can be created on. The rest of `calendarList` is
# subscriptions the account can read and not write.
WRITABLE_ROLES = frozenset({"owner", "writer"})

# How far either way an event is looked for when the user names one. Behind, so
# that "delete this morning's standup" still works in the afternoon; ahead, far
# enough to cover anything a person would refer to by name without a date.
_SEARCH_BACK_DAYS = 1
_SEARCH_AHEAD_DAYS = 365


class GoogleCalendarTools:
    """The five event tools, bound to one account's client."""

    def __init__(
        self,
        client: GoogleCalendarClient,
        *,
        default_calendar: str = "",
        timezone: str = "UTC",
    ) -> None:
        self.client = client
        self.default_calendar = default_calendar
        self.timezone = timezone

    def register(self, registry: Any) -> None:
        for tool in (
            self.list_google_events,
            self.add_google_event,
            self.edit_google_event,
            self.move_google_event,
            self.delete_google_event,
        ):
            registry.tool(tool)

    # ---- Tools ----

    def list_google_events(self, calendar_name: str = "", start: str = "", days: int = 7) -> dict:
        """List what is on the user's calendars over a stretch of days.

        Args:
            calendar_name: One calendar to read, by name. Leave empty to read
                them all, which is usually what you want.
            start: The first day to include, as YYYY-MM-DD or "today". Defaults
                to today.
            days: How many days from the start to cover. 1 is that day alone.
        """
        calendars = self._calendars()
        # Unnamed means all of them. Reading is the one place the choice can be
        # dodged rather than asked about: showing everything answers what was
        # actually asked, and it is where the model learns the calendar names.
        wanted = (
            [match_one(calendars, calendar_name, kind="calendar", title_key="summary")]
            if calendar_name.strip()
            else calendars
        )

        first = (
            parse_when(start, self.timezone, field="start day").date
            if start.strip()
            else today_in(self.timezone)
        )
        zone = ZoneInfo(self.timezone)
        time_min = datetime.combine(first, datetime.min.time(), tzinfo=zone)
        time_max = time_min + timedelta(days=max(int(days), 1))

        return {
            "from": first.isoformat(),
            "to": (time_max.date() - timedelta(days=1)).isoformat(),
            "calendars": [
                {
                    "calendar": calendar.get("summary", ""),
                    "events": [
                        self._describe(event)
                        for event in self.client.list_events(
                            calendar["id"],
                            time_min=time_min.isoformat(),
                            time_max=time_max.isoformat(),
                        )
                    ],
                }
                for calendar in wanted
            ],
        }

    def add_google_event(
        self,
        title: str,
        start: str,
        end: str,
        all_day: bool,
        location: str,
        calendar_name: str = "",
    ) -> dict:
        """Put a new event on a calendar.

        Every argument here is something only the user knows. If they have not
        said it, ask them before calling this -- do not fill it in yourself,
        and do not assume a length, a time, or that an event is all day.

        Args:
            title: What the event is called, e.g. "dentist".
            start: When it starts. "2026-08-25 14:00" or "tomorrow 2pm" for a
                timed event; "2026-08-25" or "tomorrow" for an all-day one.
            end: When it ends, in the same form as start. For an all-day event
                this is the last day it covers. If the user has not said how
                long it runs, ask them.
            all_day: True if it takes the whole day, False if it runs between
                two times. If the user has not made that clear, ask them.
            location: Where it is. Pass "none" only if the user has said there
                is no location.
            calendar_name: Which calendar to put it on, by name. Leave empty
                unless the user named one; you will be asked if it is unclear.
        """
        title = require(title, "What should the event be called?")
        calendar = self._choose(calendar_name)

        first = self._when(start, all_day, "start")
        last = self._when(end, all_day, "end")
        _check_order(first, last, all_day)

        body: dict[str, Any] = {
            "summary": title,
            "start": self._endpoint(first, all_day),
            "end": self._endpoint(last, all_day, exclusive=True),
        }
        # Required in the schema, so blank is a model that skipped the question
        # rather than a user who has nowhere in mind. The word `none` is how
        # the second of those is said.
        if not said_none(location):
            body["location"] = require(location, "Where is it happening?")

        created = self.client.insert_event(calendar["id"], body)
        return {"calendar": calendar.get("summary", ""), "added": self._describe(created)}

    def edit_google_event(
        self,
        event: str,
        title: str = "",
        start: str = "",
        end: str = "",
        location: str = "",
        calendar_name: str = "",
    ) -> dict:
        """Change an event that is already on a calendar.

        Args:
            event: The event to change, by its current title.
            title: A new title for it.
            start: A new start. Give `end` as well whenever you give this.
            end: A new end. Give `start` as well whenever you give this.
            location: A new location, or "none" to remove it.
            calendar_name: Which calendar the event is on, if you know. Leave
                empty and it is found by name across all of them.
        """
        calendar, found = self._find(event, calendar_name)

        body: dict[str, Any] = {}
        if title.strip():
            body["summary"] = title.strip()
        if location.strip():
            # Cleared with an empty string rather than null: Calendar treats a
            # null field in a PATCH as "leave it alone", so null would silently
            # do nothing where the user asked for the location to come off.
            body["location"] = "" if said_none(location) else location.strip()
        if start.strip() or end.strip():
            body.update(self._reschedule(start, end, found))

        if not body:
            raise ClarificationNeeded(
                f"What should change about {found.get('summary', event)!r} -- "
                "its name, when it is, or where?"
            )

        updated = self.client.patch_event(calendar["id"], found["id"], body)
        return {"calendar": calendar.get("summary", ""), "updated": self._describe(updated)}

    def move_google_event(self, event: str, to_calendar: str, calendar_name: str = "") -> dict:
        """Move an event to a different calendar.

        Args:
            event: The event to move, by its title.
            to_calendar: Name of the calendar to move it to. Ask the user if
                they have not said which.
            calendar_name: Which calendar it is on now, if you know. Leave
                empty and it is found by name across all of them.
        """
        calendars = self._calendars(writable_only=True)
        source, found = self._find(event, calendar_name, calendars=calendars)
        destination = match_one(
            calendars,
            require(to_calendar, f"Which calendar should {found.get('summary', event)!r} move to?"),
            kind="calendar",
            title_key="summary",
        )

        if destination["id"] == source["id"]:
            raise ToolArgumentError(
                f"{found.get('summary', event)!r} is already on {source.get('summary', '')!r}."
            )

        moved = self.client.move_event(source["id"], found["id"], destination["id"])
        return {
            "moved": self._describe(moved),
            "from_calendar": source.get("summary", ""),
            "to_calendar": destination.get("summary", ""),
        }

    def delete_google_event(self, event: str, calendar_name: str = "") -> dict:
        """Delete an event from a calendar.

        Args:
            event: The event to delete, by its title.
            calendar_name: Which calendar it is on, if you know. Leave empty
                and it is found by name across all of them.
        """
        calendar, found = self._find(event, calendar_name)
        self.client.delete_event(calendar["id"], found["id"])
        return {"deleted": found.get("summary", ""), "calendar": calendar.get("summary", "")}

    # ---- When ----

    def _when(self, text: str, all_day: bool, edge: str) -> When:
        """One end of an event, checked against what the model claimed it is.

        This is where a guess is caught. A model told only "Tuesday" will send
        a bare date; if it also said the event is *not* all-day, the two
        statements cannot both be true, and the missing piece -- what time --
        is precisely the thing only the user can supply.
        """
        when = parse_when(require(text, f"When does it {edge}?"), self.timezone, field=edge)

        if all_day and not when.all_day:
            raise ClarificationNeeded(
                f"Is this an all-day event, or does it run from a specific time? "
                f"The {edge} was given as a time ({text.strip()!r}) but it was marked all-day."
            )
        if not all_day and when.all_day:
            raise ClarificationNeeded(f"What time does it {edge}?")
        return when

    def _reschedule(self, start: str, end: str, found: dict) -> dict:
        """A new start and end for an existing event, both or neither.

        Moving one edge alone is how an event ends before it begins, and the
        model rarely knows the other edge without looking it up. All-day-ness
        is derived here rather than passed: on an edit the values are concrete,
        and a bare date *is* the statement that it is all-day.
        """
        title = found.get("summary", "it")
        first = parse_when(
            require(start, f"When should {title!r} start now?"), self.timezone, field="start"
        )
        last = parse_when(require(end, f"When should {title!r} end?"), self.timezone, field="end")
        if first.all_day != last.all_day:
            raise ClarificationNeeded(
                f"Is {title!r} all day, or between two times? The new start and end do not agree."
            )

        all_day = first.all_day
        _check_order(first, last, all_day)
        return {
            "start": self._endpoint(first, all_day),
            "end": self._endpoint(last, all_day, exclusive=True),
        }

    def _endpoint(self, when: When, all_day: bool, *, exclusive: bool = False) -> dict:
        if all_day:
            # Google's all-day end is the day *after* the last one covered.
            day = when.date + timedelta(days=1) if exclusive else when.date
            return {"date": day.isoformat()}
        return {"dateTime": when.iso(), "timeZone": self.timezone}

    # ---- Resolution ----

    def _calendars(self, *, writable_only: bool = False) -> list[dict]:
        calendars = self.client.list_calendars()
        if writable_only:
            calendars = [
                calendar
                for calendar in calendars
                if calendar.get("accessRole", "") in WRITABLE_ROLES
            ]
        if not calendars:
            raise GoogleError(
                "That Google account has no calendars MINUS can write to."
                if writable_only
                else "That Google account has no calendars."
            )
        return calendars

    def _choose(self, calendar_name: str) -> dict:
        return choose_container(
            self._calendars(writable_only=True),
            calendar_name,
            self.default_calendar,
            kind="calendar",
            setting=SETTING,
            title_key="summary",
        )

    def _find(
        self, event: str, calendar_name: str, *, calendars: list[dict] | None = None
    ) -> tuple[dict, dict]:
        """The event and the calendar it is on, searching all of them if none is named."""
        calendars = calendars if calendars is not None else self._calendars(writable_only=True)
        if calendar_name.strip():
            calendars = [match_one(calendars, calendar_name, kind="calendar", title_key="summary")]

        zone = ZoneInfo(self.timezone)
        today = today_in(self.timezone)
        time_min = datetime.combine(
            today - timedelta(days=_SEARCH_BACK_DAYS), datetime.min.time(), tzinfo=zone
        )
        time_max = datetime.combine(
            today + timedelta(days=_SEARCH_AHEAD_DAYS), datetime.min.time(), tzinfo=zone
        )

        return locate(
            calendars,
            lambda calendar: self.client.list_events(
                calendar["id"],
                time_min=time_min.isoformat(),
                time_max=time_max.isoformat(),
            ),
            require(event, "Which event did they mean?"),
            kind="event",
            title_key="summary",
            container_title_key="summary",
        )

    # ---- Describing ----

    def _describe(self, event: dict) -> dict:
        """One event as the model should see it, with the API's edges undone."""
        start = event.get("start") or {}
        end = event.get("end") or {}
        all_day = "date" in start

        described = {
            "id": event.get("id", ""),
            "title": event.get("summary", ""),
            "all_day": all_day,
        }
        if all_day:
            described["start"] = start.get("date", "")
            # Back from Google's exclusive end to the last day it covers, which
            # is the day the user named and the day they expect to hear.
            described["end"] = _inclusive_end(start.get("date", ""), end.get("date", ""))
        else:
            described["start"] = _readable(start.get("dateTime", ""))
            described["end"] = _readable(end.get("dateTime", ""))
        if event.get("location"):
            described["location"] = event["location"]
        return described


def _check_order(first: When, last: When, all_day: bool) -> None:
    """Refuse an event that ends before it starts, rather than reordering it.

    Swapping them would be a guess about which end was mistyped, and the two
    guesses put the event on different days.
    """
    if all_day:
        if last.date < first.date:
            raise ToolArgumentError("The event's last day is before its first day.")
        return

    if datetime.combine(last.date, last.time or datetime.min.time()) <= datetime.combine(
        first.date, first.time or datetime.min.time()
    ):
        raise ToolArgumentError(
            "The event ends at or before it starts. If it runs past midnight, give the "
            "end date as well as the time."
        )


def _inclusive_end(start_date: str, end_date: str) -> str:
    try:
        last = datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=1)
    except ValueError:
        return end_date or start_date
    return last.isoformat()


def _readable(timestamp: str) -> str:
    """`2026-08-25T14:00:00-05:00` as `2026-08-25 14:00`.

    The offset and the seconds are noise to a model that is about to say this
    out loud, and the zone is the assistant's own in every case it created.
    """
    if not timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    return moment.strftime("%Y-%m-%d %H:%M")


__all__ = ["GoogleCalendarTools"]
