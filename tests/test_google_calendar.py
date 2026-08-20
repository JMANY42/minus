"""Tests for the calendar tools.

Most of these are about the two things an event can be wrong in ways a task
cannot: which calendar it lands on, and whether "Tuesday" meant all day or a
time nobody stated. The all-day cross-check has a class to itself, because it
is the whole reason `all_day` is a separate required argument rather than
something derived quietly from the format of `start`.
"""

from __future__ import annotations

import json

import pytest

from minus.errors import ClarificationNeeded, GoogleError, ToolArgumentError
from minus.services.google import GoogleCredentials
from minus.services.google_calendar import GoogleCalendarClient
from minus.tools.google_calendar import GoogleCalendarTools
from minus.tools.registry import ToolRegistry
from tests.google_fake import TZ, FakeGoogle, day


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def credentials(google) -> GoogleCredentials:
    return GoogleCredentials("client-id", "client-secret", "refresh-token", http=google.client())


@pytest.fixture
def tools(credentials) -> GoogleCalendarTools:
    return GoogleCalendarTools(GoogleCalendarClient(credentials), timezone=TZ)


@pytest.fixture
def one_calendar(google, credentials) -> GoogleCalendarTools:
    """An account whose only writable calendar is the primary one."""
    google.calendars = [c for c in google.calendars if c["summary"] != "Work"]
    return GoogleCalendarTools(GoogleCalendarClient(credentials), timezone=TZ)


def _sent(google) -> dict:
    return json.loads(google.requests[-1].content)


class TestListing:
    def test_every_calendar_is_read_when_none_is_named(self, tools):
        names = [group["calendar"] for group in tools.list_google_events(days=30)["calendars"]]
        # Including the subscribed read-only one: you can read a holiday
        # calendar even though you cannot put anything on it.
        assert names == ["Personal", "Work", "Holidays in the US"]

    def test_the_window_starts_today_and_covers_the_days_asked_for(self, tools):
        result = tools.list_google_events(days=7)
        assert (result["from"], result["to"]) == (day(0), day(6))

    def test_events_outside_the_window_are_not_returned(self, tools):
        # The dentist is five days out, so a two-day window must not show it.
        near = tools.list_google_events(calendar_name="Personal", days=2)
        assert near["calendars"][0]["events"] == []

        far = tools.list_google_events(calendar_name="Personal", days=10)
        assert [event["title"] for event in far["calendars"][0]["events"]] == ["Dentist"]

    def test_a_timed_event_reads_back_without_offsets_or_seconds(self, tools):
        (dentist,) = tools.list_google_events(calendar_name="Personal", days=10)["calendars"][0][
            "events"
        ]
        assert dentist == {
            "id": "ev1",
            "title": "Dentist",
            "all_day": False,
            "start": f"{day(5)} 14:00",
            "end": f"{day(5)} 15:00",
            "location": "Main St",
        }

    def test_an_all_day_event_reads_back_with_the_last_day_it_covers(self, tools):
        # Stored with an exclusive end of day(15); the user's conference runs
        # to day(14), and that is what they expect to hear.
        events = tools.list_google_events(calendar_name="Work", days=20)["calendars"][0]["events"]
        (conference,) = [event for event in events if event["title"] == "Conference"]
        assert conference["all_day"] is True
        assert (conference["start"], conference["end"]) == (day(12), day(14))

    def test_a_start_day_can_be_given(self, tools):
        result = tools.list_google_events(calendar_name="Personal", start=day(5), days=1)
        assert [event["title"] for event in result["calendars"][0]["events"]] == ["Dentist"]


class TestChoosingTheCalendar:
    def test_two_writable_calendars_and_no_name_is_a_question(self, tools):
        with pytest.raises(ClarificationNeeded, match="Which calendar"):
            tools.add_google_event("Lunch", f"{day(1)} 12:00", f"{day(1)} 13:00", False, "none")

    def test_the_question_offers_only_calendars_that_can_be_written_to(self, tools):
        # Offering a subscribed holiday calendar as somewhere to put lunch
        # turns one real choice into a menu of three, two of which fail.
        with pytest.raises(ClarificationNeeded) as caught:
            tools.add_google_event("Lunch", f"{day(1)} 12:00", f"{day(1)} 13:00", False, "none")
        assert "Holidays" not in str(caught.value)
        assert "'Personal', 'Work'" in str(caught.value)

    def test_a_single_writable_calendar_is_used_without_asking(self, one_calendar):
        result = one_calendar.add_google_event(
            "Lunch", f"{day(1)} 12:00", f"{day(1)} 13:00", False, "none"
        )
        assert result["calendar"] == "Personal"

    def test_a_configured_default_is_used_without_asking(self, credentials):
        tools = GoogleCalendarTools(
            GoogleCalendarClient(credentials), default_calendar="Work", timezone=TZ
        )
        result = tools.add_google_event(
            "Lunch", f"{day(1)} 12:00", f"{day(1)} 13:00", False, "none"
        )
        assert result["calendar"] == "Work"

    def test_a_configured_default_that_does_not_exist_is_reported(self, credentials):
        tools = GoogleCalendarTools(
            GoogleCalendarClient(credentials), default_calendar="Gone", timezone=TZ
        )
        with pytest.raises(ToolArgumentError, match="MINUS_GOOGLE_CALENDAR"):
            tools.add_google_event("Lunch", f"{day(1)} 12:00", f"{day(1)} 13:00", False, "none")


class TestAllDayOrTimed:
    """The cross-check: what the model claimed, against what it actually sent."""

    def test_a_timed_event_with_no_time_is_a_question(self, one_calendar):
        # The exact shape of a guess: the user said "Tuesday", the model knows
        # it is not all-day, and it has no time to put in.
        with pytest.raises(ClarificationNeeded, match="What time does it start"):
            one_calendar.add_google_event("Dentist", day(2), day(2), False, "none")

    def test_a_missing_end_time_is_a_question_even_when_the_start_has_one(self, one_calendar):
        with pytest.raises(ClarificationNeeded, match="What time does it end"):
            one_calendar.add_google_event("Dentist", f"{day(2)} 14:00", day(2), False, "none")

    def test_an_all_day_event_carrying_a_time_is_a_question(self, one_calendar):
        # The contradiction the other way. Neither half is trusted over the
        # other, because either one alone is exactly what a guess looks like.
        with pytest.raises(ClarificationNeeded, match="all-day event, or does it run"):
            one_calendar.add_google_event(
                "Conference", f"{day(2)} 09:00", f"{day(4)} 17:00", True, "none"
            )

    def test_a_blank_start_is_a_question(self, one_calendar):
        with pytest.raises(ClarificationNeeded, match="When does it start"):
            one_calendar.add_google_event("Dentist", "", f"{day(2)} 15:00", False, "none")

    def test_a_blank_end_is_a_question_rather_than_an_assumed_hour(self, one_calendar):
        with pytest.raises(ClarificationNeeded, match="When does it end"):
            one_calendar.add_google_event("Dentist", f"{day(2)} 14:00", "", False, "none")


class TestAdding:
    def test_a_timed_event_is_sent_with_the_configured_zone(self, one_calendar, google):
        result = one_calendar.add_google_event(
            "Dentist", f"{day(2)} 14:00", f"{day(2)} 15:00", False, "Main St"
        )
        assert _sent(google) == {
            "summary": "Dentist",
            "start": {"dateTime": f"{day(2)}T14:00:00", "timeZone": TZ},
            "end": {"dateTime": f"{day(2)}T15:00:00", "timeZone": TZ},
            "location": "Main St",
        }
        assert result["added"]["all_day"] is False

    def test_an_all_day_event_is_sent_with_googles_exclusive_end(self, one_calendar, google):
        # The user said the 2nd to the 4th; Google is told the 2nd to the 5th.
        one_calendar.add_google_event("Conference", day(2), day(4), True, "none")
        assert _sent(google)["start"] == {"date": day(2)}
        assert _sent(google)["end"] == {"date": day(5)}

    def test_a_one_day_all_day_event_still_ends_the_day_after(self, one_calendar, google):
        one_calendar.add_google_event("Day off", day(2), day(2), True, "none")
        assert _sent(google)["end"] == {"date": day(3)}

    def test_relative_days_and_bare_times_are_understood(self, one_calendar, google):
        one_calendar.add_google_event("Lunch", "tomorrow 12pm", "tomorrow 1pm", False, "none")
        assert _sent(google)["start"] == {"dateTime": f"{day(1)}T12:00:00", "timeZone": TZ}
        assert _sent(google)["end"] == {"dateTime": f"{day(1)}T13:00:00", "timeZone": TZ}

    def test_none_means_the_user_said_there_is_no_location(self, one_calendar, google):
        one_calendar.add_google_event("Call", f"{day(1)} 09:00", f"{day(1)} 09:30", False, "none")
        assert "location" not in _sent(google)

    def test_a_blank_location_is_a_question_rather_than_no_location(self, one_calendar):
        with pytest.raises(ClarificationNeeded, match="Where is it"):
            one_calendar.add_google_event("Call", f"{day(1)} 09:00", f"{day(1)} 09:30", False, "")

    def test_a_blank_title_is_a_question(self, one_calendar):
        with pytest.raises(ClarificationNeeded, match="called"):
            one_calendar.add_google_event("  ", f"{day(1)} 09:00", f"{day(1)} 09:30", False, "none")

    def test_an_event_that_ends_before_it_starts_is_refused(self, one_calendar):
        with pytest.raises(ToolArgumentError, match="ends at or before it starts"):
            one_calendar.add_google_event(
                "Backwards", f"{day(1)} 15:00", f"{day(1)} 14:00", False, "none"
            )

    def test_an_all_day_event_ending_before_it_starts_is_refused(self, one_calendar):
        with pytest.raises(ToolArgumentError, match="last day is before its first day"):
            one_calendar.add_google_event("Backwards", day(4), day(2), True, "none")


class TestEditing:
    def test_a_new_title_is_patched_and_nothing_else_is_sent(self, tools, google):
        tools.edit_google_event("Dentist", title="Orthodontist")
        assert _sent(google) == {"summary": "Orthodontist"}

    def test_rescheduling_sends_both_ends(self, tools, google):
        tools.edit_google_event("Dentist", start=f"{day(6)} 10:00", end=f"{day(6)} 11:00")
        assert _sent(google) == {
            "start": {"dateTime": f"{day(6)}T10:00:00", "timeZone": TZ},
            "end": {"dateTime": f"{day(6)}T11:00:00", "timeZone": TZ},
        }

    def test_moving_only_one_end_asks_for_the_other(self, tools):
        # Moving one edge alone is how an event ends before it begins.
        with pytest.raises(ClarificationNeeded, match="When should 'Dentist' end"):
            tools.edit_google_event("Dentist", start=f"{day(6)} 10:00")

    def test_a_new_start_and_end_that_disagree_about_all_day_is_a_question(self, tools):
        with pytest.raises(ClarificationNeeded, match="all day, or between two times"):
            tools.edit_google_event("Dentist", start=day(6), end=f"{day(6)} 11:00")

    def test_an_event_can_be_turned_into_an_all_day_one(self, tools, google):
        # Two bare dates say all-day without a flag, because on an edit the
        # values are concrete rather than something the model inferred.
        tools.edit_google_event("Dentist", start=day(6), end=day(6))
        assert _sent(google) == {"start": {"date": day(6)}, "end": {"date": day(7)}}

    def test_none_clears_the_location_with_an_empty_string(self, tools, google):
        # Calendar reads a null in a PATCH as "leave it alone", so null would
        # silently do nothing where the user asked for the location to come off.
        tools.edit_google_event("Dentist", location="none")
        assert _sent(google) == {"location": ""}

    def test_an_edit_that_changes_nothing_asks_what_to_change(self, tools):
        with pytest.raises(ClarificationNeeded, match="What should change"):
            tools.edit_google_event("Dentist")


class TestMoving:
    def test_an_event_moves_to_the_named_calendar(self, tools, google):
        result = tools.move_google_event("Dentist", to_calendar="Work")
        assert (result["from_calendar"], result["to_calendar"]) == ("Personal", "Work")
        assert google.requests[-1].url.params["destination"] == "work@group.calendar.google.com"
        assert "ev1" in [event["id"] for event in google.events["work@group.calendar.google.com"]]

    def test_a_move_with_no_destination_is_a_question(self, tools):
        with pytest.raises(ClarificationNeeded, match="Which calendar"):
            tools.move_google_event("Dentist", to_calendar="")

    def test_a_move_to_where_it_already_is_is_refused(self, tools):
        with pytest.raises(ToolArgumentError, match="already on"):
            tools.move_google_event("Dentist", to_calendar="Personal")

    def test_a_read_only_calendar_is_not_a_destination(self, tools):
        with pytest.raises(ToolArgumentError, match="no calendar called"):
            tools.move_google_event("Dentist", to_calendar="Holidays in the US")


class TestDeleting:
    def test_an_event_is_deleted_and_its_real_title_reported(self, tools, google):
        assert tools.delete_google_event("dentist") == {
            "deleted": "Dentist",
            "calendar": "Personal",
        }
        assert google.events["me@example.com"] == []

    def test_an_event_is_found_across_calendars_without_being_told_which(self, tools):
        # One match on the account is an answer, not a guess.
        assert tools.delete_google_event("Conference")["calendar"] == "Work"

    def test_an_ambiguous_reference_asks_and_says_where_each_one_is(self, tools, google):
        google.events["work@group.calendar.google.com"].append(
            {
                "id": "ev9",
                "summary": "Dentist",
                "start": {"date": day(6)},
                "end": {"date": day(7)},
            }
        )
        with pytest.raises(ClarificationNeeded, match="'Dentist' on 'Personal'"):
            tools.delete_google_event("Dentist")

    def test_naming_the_calendar_settles_an_otherwise_ambiguous_event(self, tools, google):
        google.events["work@group.calendar.google.com"].append(
            {
                "id": "ev9",
                "summary": "Dentist",
                "start": {"date": day(6)},
                "end": {"date": day(7)},
            }
        )
        assert tools.delete_google_event("Dentist", calendar_name="Work")["calendar"] == "Work"

    def test_an_event_on_a_read_only_calendar_is_not_found(self, tools):
        # Nothing can be done to it, so offering it as a match would only lead
        # to a 403 two calls later.
        with pytest.raises(ToolArgumentError, match="no event called"):
            tools.delete_google_event("Labor Day")

    def test_an_unknown_event_says_where_it_looked(self, tools):
        with pytest.raises(ToolArgumentError, match="'Personal', 'Work'"):
            tools.delete_google_event("Coronation")

    def test_an_account_with_no_writable_calendars_says_so(self, tools, google):
        google.calendars = [c for c in google.calendars if c["accessRole"] == "reader"]
        with pytest.raises(GoogleError, match="no calendars MINUS can write to"):
            tools.delete_google_event("Dentist")


class TestRegistration:
    def test_the_five_tools_register_with_schemas_from_their_signatures(self, tools):
        registry = ToolRegistry()
        tools.register(registry)

        assert registry.names() == [
            "add_google_event",
            "delete_google_event",
            "edit_google_event",
            "list_google_events",
            "move_google_event",
        ]
        schema = registry.get("add_google_event").schema["function"]
        # Every field the user owns is required, so the model cannot omit one
        # and have a default quietly chosen for it. The calendar is the
        # exception: only the resolution rules know if it needs asking about.
        assert schema["parameters"]["required"] == [
            "title",
            "start",
            "end",
            "all_day",
            "location",
        ]
        assert schema["parameters"]["properties"]["all_day"]["type"] == "boolean"

    def test_dispatch_runs_them_end_to_end(self, tools):
        registry = ToolRegistry()
        tools.register(registry)

        result = json.loads(
            registry.dispatch("list_google_events", {"calendar_name": "Personal", "days": 10})
        )
        assert [event["title"] for event in result["calendars"][0]["events"]] == ["Dentist"]
