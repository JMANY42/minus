"""The Google Tasks tools: title, due date, list, and no guessing.

Three fields, because three is what the user cares about. Notes, subtasks and
ordering were all reachable through this API and are all gone from it: every
extra parameter is another line of schema in front of a small fast model, and
another field it can quietly fill in wrong.

Those three are also exactly the fields it is *not* allowed to invent, which is
what shapes the signatures here. `title` and `due` are required parameters, so
the model cannot omit them and quietly create a task with no deadline -- and
because "the user said there is no due date" is a real answer, `due` takes the
word `none` for it. The list is not a required parameter, because the model
cannot know what `.env` configured or how many lists exist; it is resolved by
`choose_container`, which asks only when there is genuinely something to
choose. See `google_shared.py` for the rule in full.

Constructed rather than registered at import, the way `escalate` is: the
credentials come from `Settings`, and `assembly.py` is the only module allowed
to read those.

Deliberately not in `DEEP_TOOL_NAMES`. The deep tier runs unattended on a
background thread, and deleting someone's tasks is not something to discover
afterwards in a write-up.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.errors import ClarificationNeeded, GoogleError, ToolArgumentError
from minus.services.google_tasks import GoogleTasksClient
from minus.tools.google_shared import (
    When,
    choose_container,
    locate,
    match_one,
    parse_when,
    require,
    said_none,
)

logger = logging.getLogger(__name__)

# Google stores `due` as an RFC 3339 timestamp but only honours the date part;
# a time of day set here is accepted, kept, and never shown to the user. So a
# date is what goes in, and a date is what comes back out.
_DUE_SUFFIX = "T00:00:00.000Z"

SETTING = "MINUS_GOOGLE_TASKS_LIST"

COMPLETED = "completed"
NEEDS_ACTION = "needsAction"
_STATUSES = {
    COMPLETED: COMPLETED,
    "complete": COMPLETED,
    "done": COMPLETED,
    "needs_action": NEEDS_ACTION,
    NEEDS_ACTION.lower(): NEEDS_ACTION,
    "todo": NEEDS_ACTION,
    "incomplete": NEEDS_ACTION,
    "undone": NEEDS_ACTION,
}


class GoogleTasksTools:
    """The five task tools, bound to one account's client."""

    def __init__(
        self,
        client: GoogleTasksClient,
        *,
        default_list: str = "",
        timezone: str = "UTC",
    ) -> None:
        self.client = client
        self.default_list = default_list
        self.timezone = timezone

    def register(self, registry: Any) -> None:
        """Attach these tools to a registry, as `escalate` is attached.

        Bound methods, so the derived schema comes from the signature and
        docstring exactly as it does for a module-level tool -- `self` is
        already bound and never appears in the parameters.
        """
        for tool in (
            self.list_google_tasks,
            self.add_google_task,
            self.edit_google_task,
            self.move_google_task,
            self.delete_google_task,
        ):
            registry.tool(tool)

    # ---- Tools ----

    def list_google_tasks(self, list_name: str = "", include_completed: bool = False) -> dict:
        """List the tasks on the user's Google Tasks lists.

        Args:
            list_name: One list to read, by name. Leave empty to read them all,
                which is usually what you want -- reading everything is never
                wrong, and it shows you the list names.
            include_completed: Also return tasks already ticked off. Off by
                default, because the open ones are what gets asked for.
        """
        lists = self._tasklists()
        # Unnamed means every list, not a chosen one. Reading is the one place
        # the choice can be dodged instead of asked about: showing all of them
        # answers the question the user actually asked, and it is where the
        # model learns the names it will need for everything below.
        wanted = [match_one(lists, list_name, kind="task list")] if list_name.strip() else lists

        return {
            "lists": [
                {
                    "list": tasklist.get("title", ""),
                    "tasks": [
                        _describe(task)
                        for task in self.client.list_tasks(
                            tasklist["id"], show_completed=include_completed
                        )
                    ],
                }
                for tasklist in wanted
            ]
        }

    def add_google_task(self, title: str, due: str, list_name: str = "") -> dict:
        """Add a task to a Google Tasks list.

        Every argument here is something only the user knows. If they have not
        said it, ask them before calling this -- do not fill it in yourself.

        Args:
            title: What the task says, e.g. "buy milk". Ask if it is not clear.
            due: When it is due, as YYYY-MM-DD, "today" or "tomorrow". If the
                user has not said when, ask them. Only pass "none" if they have
                actually said it has no deadline.
            list_name: Which list to add it to, by name. Leave empty unless the
                user named one; the list is worked out from their configuration
                and you will be asked if it is unclear.
        """
        title = require(title, "What should the task be called?")
        chosen = self._choose(list_name)

        body: dict[str, Any] = {"title": title}
        # Required in the schema, so a blank one is a model that skipped the
        # question rather than a user who has no deadline in mind. The word
        # `none` is how the second of those is said.
        if not said_none(due):
            body["due"] = self._parse_due(due)

        created = self.client.insert_task(chosen["id"], body)
        return {"list": chosen.get("title", ""), "added": _describe(created)}

    def edit_google_task(
        self,
        task: str,
        title: str = "",
        due: str = "",
        status: str = "",
        list_name: str = "",
    ) -> dict:
        """Change a task that already exists.

        Args:
            task: The task to change, by its current title.
            title: A new title for it.
            due: A new due date, as YYYY-MM-DD, "today" or "tomorrow", or
                "none" to remove the deadline. Leave empty to leave it alone.
            status: "completed" to tick it off, or "needs_action" to reopen it.
            list_name: Which list the task is on, if you know. Leave empty and
                it is found by name across all of them.
        """
        tasklist, found = self._find(task, list_name)

        body: dict[str, Any] = {}
        if title.strip():
            body["title"] = title.strip()
        if due.strip():
            # A cleared field is sent as null, not as "": the API takes an
            # empty string literally and leaves an empty value on the task.
            body["due"] = None if said_none(due) else self._parse_due(due)
        if status.strip():
            body["status"] = _parse_status(status)
            if body["status"] == NEEDS_ACTION:
                # Reopening a task that Google marked complete leaves
                # `completed` set unless it is explicitly cleared, and a task
                # holding both states is rendered as still done.
                body["completed"] = None

        if not body:
            raise ClarificationNeeded(
                f"What should change about {found.get('title', task)!r} -- "
                "its name, when it is due, or whether it is done?"
            )

        updated = self.client.patch_task(tasklist["id"], found["id"], body)
        return {"list": tasklist.get("title", ""), "updated": _describe(updated)}

    def move_google_task(self, task: str, to_list: str, list_name: str = "") -> dict:
        """Move a task to a different list.

        Args:
            task: The task to move, by its title.
            to_list: Name of the list to move it to. Ask the user if they have
                not said which.
            list_name: Which list the task is on now, if you know. Leave empty
                and it is found by name across all of them.
        """
        lists = self._tasklists()
        source, found = self._find(task, list_name, lists=lists)
        destination = match_one(
            lists,
            require(to_list, f"Which list should {found.get('title', task)!r} move to?"),
            kind="task list",
        )

        if destination["id"] == source["id"]:
            raise ToolArgumentError(
                f"{found.get('title', task)!r} is already on {source.get('title', '')!r}."
            )

        moved = self.client.move_task(
            source["id"], found["id"], destination_tasklist=destination["id"]
        )
        return {
            "moved": _describe(moved),
            "from_list": source.get("title", ""),
            "to_list": destination.get("title", ""),
        }

    def delete_google_task(self, task: str, list_name: str = "") -> dict:
        """Delete a task.

        Args:
            task: The task to delete, by its title.
            list_name: Which list the task is on, if you know. Leave empty and
                it is found by name across all of them.
        """
        tasklist, found = self._find(task, list_name)
        self.client.delete_task(tasklist["id"], found["id"])
        # The title goes back in the result so the model can confirm what went
        # rather than repeat the words it was given -- which is the difference
        # between "deleted buy milk" and having deleted something else.
        return {"deleted": found.get("title", ""), "list": tasklist.get("title", "")}

    # ---- Resolution ----

    def _tasklists(self) -> list[dict]:
        lists = self.client.list_tasklists()
        if not lists:
            raise GoogleError("That Google account has no task lists.")
        return lists

    def _choose(self, list_name: str) -> dict:
        return choose_container(
            self._tasklists(),
            list_name,
            self.default_list,
            kind="task list",
            setting=SETTING,
        )

    def _find(
        self, task: str, list_name: str, *, lists: list[dict] | None = None
    ) -> tuple[dict, dict]:
        """The task and the list it is on, searching every list if none is named.

        Completed ones are included: "reopen the one I finished yesterday" is a
        real request, and a task the model cannot see it cannot name.
        """
        lists = lists if lists is not None else self._tasklists()
        if list_name.strip():
            lists = [match_one(lists, list_name, kind="task list")]

        return locate(
            lists,
            lambda tasklist: self.client.list_tasks(tasklist["id"], show_completed=True),
            require(task, "Which task did they mean?"),
            kind="task",
        )

    def _parse_due(self, due: str) -> str:
        when: When = parse_when(due, self.timezone, field="due date")
        return f"{when.date.isoformat()}{_DUE_SUFFIX}"


def _parse_status(status: str) -> str:
    try:
        return _STATUSES[status.strip().casefold().replace(" ", "_")]
    except KeyError:
        raise ToolArgumentError(
            f"Unknown status {status!r}. Use 'completed' or 'needs_action'."
        ) from None


def _describe(task: dict) -> dict:
    """One task as the model should see it: named fields, no API bookkeeping.

    The id travels along because the model is expected to be able to act on
    what it just listed, and passing the title back would re-run the resolution
    above against a list that may hold two of them by then.
    """
    described = {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "status": COMPLETED if task.get("status") == COMPLETED else "needs_action",
    }
    # Only what is actually set: an empty due date spoken aloud as "due,
    # nothing" is noise.
    if task.get("due"):
        described["due"] = str(task["due"])[:10]
    return described


__all__ = ["GoogleTasksTools"]
