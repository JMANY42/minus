"""Tests for the task tools: what they send, and what they refuse to invent.

The refusals are the point of most of this file. A tool that quietly picks a
list, or files a task with no deadline because nobody mentioned one, is the
failure being guarded against -- so the assertions are as much about
`ClarificationNeeded` being raised as about the happy path working.
"""

from __future__ import annotations

import json

import pytest

from minus.errors import (
    ClarificationNeeded,
    GoogleAuthError,
    GoogleError,
    ToolArgumentError,
)
from minus.services.google import GoogleCredentials
from minus.services.google_tasks import GoogleTasksClient
from minus.tools.google_tasks import GoogleTasksTools
from minus.tools.registry import ToolRegistry
from tests.google_fake import TZ, FakeGoogle, day


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def credentials(google) -> GoogleCredentials:
    return GoogleCredentials("client-id", "client-secret", "refresh-token", http=google.client())


@pytest.fixture
def tools(credentials) -> GoogleTasksTools:
    return GoogleTasksTools(GoogleTasksClient(credentials), timezone=TZ)


@pytest.fixture
def one_list(google, credentials) -> GoogleTasksTools:
    """An account with a single list, where there is nothing to ask about."""
    google.lists = [{"id": "list-1", "title": "My Tasks"}]
    return GoogleTasksTools(GoogleTasksClient(credentials), timezone=TZ)


class TestCredentials:
    def test_missing_credentials_are_refused_at_construction(self):
        # Rather than at the first call, which would be inside a tool the model
        # had already been offered.
        with pytest.raises(GoogleAuthError, match="MINUS_GOOGLE_CLIENT_ID"):
            GoogleCredentials("id", "secret", "")

    def test_the_access_token_is_fetched_once_and_shared(self, credentials, google):
        # One grant, one token, however many APIs are built on it.
        GoogleTasksClient(credentials).list_tasklists()
        GoogleTasksClient(credentials).list_tasklists()
        assert google.token_calls == 1

    def test_a_rejected_token_is_refreshed_and_the_call_retried_once(self, credentials, google):
        google.reject_tokens = 1
        assert GoogleTasksClient(credentials).list_tasklists()
        assert google.token_calls == 2

    def test_a_token_rejected_twice_is_reported_rather_than_looped_on(self, credentials, google):
        google.reject_tokens = 5
        with pytest.raises(GoogleError):
            GoogleTasksClient(credentials).list_tasklists()
        assert google.token_calls == 2

    def test_a_revoked_refresh_token_says_so(self, google):
        credentials = GoogleCredentials("id", "secret", "revoked", http=google.client())
        with pytest.raises(GoogleAuthError, match="invalid_grant"):
            GoogleTasksClient(credentials).list_tasklists()

    def test_a_grant_predating_a_scope_names_the_remedy(self, credentials, google):
        # The state every existing install is in the first time it reaches for
        # a calendar. "Insufficient authentication scopes" sounds transient and
        # is not, so the message says what to actually do.
        google.forbid = {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Request had insufficient authentication scopes.",
                "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
            }
        }
        with pytest.raises(GoogleAuthError, match="minus google-auth"):
            GoogleTasksClient(credentials).list_tasklists()


class TestListing:
    def test_every_list_is_read_when_none_is_named(self, tools):
        # Reading is the one place the choice can be dodged rather than asked
        # about: showing everything is never the wrong answer.
        result = tools.list_google_tasks()
        assert [group["list"] for group in result["lists"]] == ["My Tasks", "Weekend"]

    def test_a_named_list_is_matched_case_insensitively(self, tools):
        result = tools.list_google_tasks(list_name="weekend")
        assert [group["list"] for group in result["lists"]] == ["Weekend"]

    def test_completed_tasks_are_left_out_unless_asked_for(self, tools):
        titles = [t["title"] for t in tools.list_google_tasks()["lists"][0]["tasks"]]
        assert "Old thing" not in titles

        with_done = tools.list_google_tasks(include_completed=True)["lists"][0]["tasks"]
        assert "Old thing" in [task["title"] for task in with_done]

    def test_an_unknown_list_names_the_ones_that_exist(self, tools):
        with pytest.raises(ToolArgumentError, match="'My Tasks', 'Weekend'"):
            tools.list_google_tasks(list_name="Groceries")

    def test_only_the_fields_a_task_actually_has_come_back(self, tools):
        tasks = tools.list_google_tasks(list_name="My Tasks")["lists"][0]["tasks"]
        (milk,) = [task for task in tasks if task["title"] == "Buy milk"]
        assert milk == {"id": "t1", "title": "Buy milk", "status": "needs_action"}


class TestChoosingTheList:
    """Which list, and when MINUS is allowed to decide that for itself."""

    def test_two_lists_and_no_name_is_a_question_not_a_guess(self, tools, google):
        before = len(google.tasks["list-1"])
        with pytest.raises(ClarificationNeeded, match="Which task list"):
            tools.add_google_task("Buy bread", due="none")
        assert len(google.tasks["list-1"]) == before

    def test_the_question_names_the_options(self, tools):
        with pytest.raises(ClarificationNeeded, match="'My Tasks', 'Weekend'"):
            tools.add_google_task("Buy bread", due="none")

    def test_a_single_list_is_used_without_asking(self, one_list):
        # Nothing to choose between, so nothing to ask about.
        assert one_list.add_google_task("Buy bread", due="none")["list"] == "My Tasks"

    def test_a_configured_default_is_used_without_asking(self, credentials):
        # A stated preference is not a guess.
        tools = GoogleTasksTools(
            GoogleTasksClient(credentials), default_list="Weekend", timezone=TZ
        )
        assert tools.add_google_task("Buy bread", due="none")["list"] == "Weekend"

    def test_a_configured_default_that_does_not_exist_is_reported(self, credentials):
        tools = GoogleTasksTools(GoogleTasksClient(credentials), default_list="Gone", timezone=TZ)
        with pytest.raises(ToolArgumentError, match="MINUS_GOOGLE_TASKS_LIST"):
            tools.add_google_task("Buy bread", due="none")

    def test_a_named_list_always_wins(self, tools):
        assert (
            tools.add_google_task("Buy bread", due="none", list_name="Weekend")["list"] == "Weekend"
        )


class TestAdding:
    def test_a_task_is_created_with_its_due_date(self, tools, google):
        result = tools.add_google_task("Book tickets", due=day(3), list_name="My Tasks")
        assert result["added"]["title"] == "Book tickets"
        assert result["added"]["due"] == day(3)
        assert json.loads(google.requests[-1].content) == {
            "title": "Book tickets",
            "due": f"{day(3)}T00:00:00.000Z",
        }

    def test_none_means_the_user_said_there_is_no_deadline(self, tools, google):
        tools.add_google_task("Someday", due="none", list_name="My Tasks")
        assert json.loads(google.requests[-1].content) == {"title": "Someday"}

    def test_a_blank_due_date_is_a_question_rather_than_no_deadline(self, tools, google):
        # The distinction the word "none" exists for: an empty string is a model
        # that never asked, and filing the task without a deadline would bury
        # that.
        before = len(google.tasks["list-1"])
        with pytest.raises(ClarificationNeeded, match="due date"):
            tools.add_google_task("Book tickets", due="", list_name="My Tasks")
        assert len(google.tasks["list-1"]) == before

    def test_a_blank_title_is_a_question(self, tools):
        with pytest.raises(ClarificationNeeded, match="called"):
            tools.add_google_task("   ", due="none", list_name="My Tasks")

    def test_today_and_tomorrow_are_a_day_apart(self, tools):
        first = tools.add_google_task("A", due="today", list_name="My Tasks")["added"]["due"]
        second = tools.add_google_task("B", due="tomorrow", list_name="My Tasks")["added"]["due"]
        assert (first, second) == (day(0), day(1))

    def test_an_unreadable_date_is_refused_before_anything_is_created(self, tools, google):
        before = len(google.tasks["list-1"])
        with pytest.raises(ToolArgumentError, match="YYYY-MM-DD"):
            tools.add_google_task("A", due="next Thursday", list_name="My Tasks")
        assert len(google.tasks["list-1"]) == before


class TestEditing:
    def test_a_new_title_is_patched_and_nothing_else_is_sent(self, tools, google):
        tools.edit_google_task("Buy milk", title="Buy oat milk")
        assert json.loads(google.requests[-1].content) == {"title": "Buy oat milk"}

    def test_completing_a_task_sets_the_status(self, tools, google):
        result = tools.edit_google_task("Buy milk", status="done")
        assert json.loads(google.requests[-1].content) == {"status": "completed"}
        assert result["updated"]["status"] == "completed"

    def test_reopening_a_task_also_clears_its_completion_time(self, tools, google):
        tools.edit_google_task("Old thing", status="needs_action")
        assert json.loads(google.requests[-1].content) == {
            "status": "needsAction",
            "completed": None,
        }

    def test_none_clears_the_due_date_rather_than_setting_it_to_the_word(self, tools, google):
        tools.edit_google_task("Buy milk", due="none")
        assert json.loads(google.requests[-1].content) == {"due": None}

    def test_an_edit_that_changes_nothing_asks_what_to_change(self, tools):
        with pytest.raises(ClarificationNeeded, match="What should change"):
            tools.edit_google_task("Buy milk")

    def test_an_unknown_status_is_refused(self, tools):
        with pytest.raises(ToolArgumentError, match="Unknown status"):
            tools.edit_google_task("Buy milk", status="nearly")


class TestMoving:
    def test_a_task_moves_to_the_named_list(self, tools, google):
        result = tools.move_google_task("Buy milk", to_list="Weekend")
        assert (result["from_list"], result["to_list"]) == ("My Tasks", "Weekend")
        assert google.requests[-1].url.params["destinationTasklist"] == "list-2"
        assert [task["id"] for task in google.tasks["list-2"]] == ["t5", "t1"]

    def test_a_move_with_no_destination_is_a_question(self, tools):
        with pytest.raises(ClarificationNeeded, match="Which list"):
            tools.move_google_task("Buy milk", to_list="")

    def test_a_move_to_where_it_already_is_is_refused(self, tools):
        with pytest.raises(ToolArgumentError, match="already on"):
            tools.move_google_task("Buy milk", to_list="My Tasks")


class TestDeleting:
    def test_a_task_is_deleted_and_its_real_title_reported(self, tools, google):
        result = tools.delete_google_task("buy milk")
        assert result == {"deleted": "Buy milk", "list": "My Tasks"}
        assert [task["id"] for task in google.tasks["list-1"]] == ["t2", "t3", "t4"]

    def test_a_completed_task_can_still_be_deleted(self, tools, google):
        tools.delete_google_task("Old thing")
        assert "t4" not in [task["id"] for task in google.tasks["list-1"]]


class TestFindingATaskByName:
    def test_a_task_is_found_across_lists_without_being_told_which(self, tools):
        # One match on the whole account is an answer, not a guess -- so this
        # does not ask which list, even though two exist.
        assert tools.delete_google_task("Wash the car")["list"] == "Weekend"

    def test_an_exact_title_beats_a_longer_one_containing_it(self, tools):
        # "Call mum" is also a substring of "Call mum's dentist"; the exact
        # match has to win, or the longer task would make the shorter one
        # unaddressable.
        assert tools.delete_google_task("Call mum")["deleted"] == "Call mum"

    def test_an_ambiguous_reference_asks_and_says_where_each_one_is(self, tools, google):
        google.tasks["list-2"].append({"id": "t6", "title": "Buy milk", "position": "1"})
        with pytest.raises(ClarificationNeeded, match="'Buy milk' on 'My Tasks'"):
            tools.delete_google_task("Buy milk")

    def test_naming_the_list_settles_an_otherwise_ambiguous_task(self, tools, google):
        google.tasks["list-2"].append({"id": "t6", "title": "Buy milk", "position": "1"})
        assert tools.delete_google_task("Buy milk", list_name="Weekend")["list"] == "Weekend"

    def test_an_id_is_accepted_as_well_as_a_title(self, tools):
        assert tools.delete_google_task("t1")["deleted"] == "Buy milk"

    def test_an_unknown_task_lists_where_it_was_looked_for(self, tools):
        with pytest.raises(ToolArgumentError, match="no task called 'Feed the cat'"):
            tools.delete_google_task("Feed the cat")

    def test_an_account_with_no_lists_says_so(self, tools, google):
        google.lists = []
        with pytest.raises(GoogleError, match="no task lists"):
            tools.list_google_tasks()


class TestRegistration:
    def test_the_five_tools_register_with_schemas_from_their_signatures(self, tools):
        registry = ToolRegistry()
        tools.register(registry)

        assert registry.names() == [
            "add_google_task",
            "delete_google_task",
            "edit_google_task",
            "list_google_tasks",
            "move_google_task",
        ]
        schema = registry.get("add_google_task").schema["function"]
        # Title and due are required so the model cannot omit them and quietly
        # file a task with no deadline. The list is not, because only the
        # resolution rules know whether it can be settled without asking.
        assert schema["parameters"]["required"] == ["title", "due"]
        assert "self" not in schema["parameters"]["properties"]

    def test_dispatch_runs_them_end_to_end(self, tools):
        registry = ToolRegistry()
        tools.register(registry)

        result = json.loads(registry.dispatch("list_google_tasks", {"list_name": "Weekend"}))
        assert [task["title"] for task in result["lists"][0]["tasks"]] == ["Wash the car"]


class TestWiring:
    """The composition root's gate: no credentials, no tools and no prompt."""

    def test_no_credentials_means_no_tools_rather_than_failing_ones(self):
        from minus.assembly import build_google_tools
        from minus.config import Settings

        settings = Settings(google_client_id="", google_client_secret="", google_refresh_token="")
        assert build_google_tools(settings) == []

    def test_credentials_produce_both_tool_groups_over_one_grant(self):
        from minus.assembly import build_google_tools
        from minus.config import Settings

        tasks, calendar = build_google_tools(
            Settings(
                google_client_id="a",
                google_client_secret="b",
                google_refresh_token="c",
                google_tasks_list="Weekend",
                google_calendar="Work",
                timezone="Europe/Berlin",
            )
        )
        assert (tasks.default_list, tasks.timezone) == ("Weekend", "Europe/Berlin")
        assert (calendar.default_calendar, calendar.timezone) == ("Work", "Europe/Berlin")
        # One grant, so one refresh token being spent and one view of whether
        # it has been revoked.
        assert tasks.client.credentials is calendar.client.credentials

    def test_the_scheduling_rules_reach_the_prompt_only_when_the_tools_do(self):
        from pathlib import Path

        from minus.prompts import build_system_prompt

        connected = build_system_prompt(Path("/w"), can_schedule=True)
        assert "Never fill in a value because it seems likely" in connected
        # Standing instructions about a calendar nobody connected are noise in
        # front of every turn.
        assert "Never fill in a value" not in build_system_prompt(Path("/w"))
