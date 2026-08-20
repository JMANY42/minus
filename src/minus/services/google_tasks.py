"""The Google Tasks REST API.

Six calls over the shared grant in `services/google.py`. This class knows about
HTTP and nothing about tools: resolving a spoken list name to an id, or a task
title to a task, is `tools/google_tasks.py` -- the split is what lets that
resolution be tested without a network at all.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.services.google import GoogleCredentials

logger = logging.getLogger(__name__)

API_ROOT = "https://tasks.googleapis.com/tasks/v1"


class GoogleTasksClient:
    """One Google account's task lists, over HTTP."""

    def __init__(self, credentials: GoogleCredentials) -> None:
        self.credentials = credentials

    def request(self, method: str, path: str, **kwargs: Any) -> dict:
        return self.credentials.request(method, f"{API_ROOT}{path}", **kwargs)

    # ---- Task lists ----

    def list_tasklists(self) -> list[dict]:
        """Every list on the account, in Google's own order."""
        payload = self.request("GET", "/users/@me/lists", params={"maxResults": 100})
        return list(payload.get("items") or [])

    # ---- Tasks ----

    def list_tasks(
        self,
        tasklist_id: str,
        *,
        show_completed: bool = False,
        max_results: int = 100,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "maxResults": max_results,
            "showCompleted": show_completed,
            # Completed tasks are *hidden* as well as completed once the user
            # clears them in the Google app, so asking for completed ones
            # without this returns almost nothing.
            "showHidden": show_completed,
        }
        payload = self.request("GET", f"/lists/{tasklist_id}/tasks", params=params)
        items = list(payload.get("items") or [])
        # `position` is a zero-padded string precisely so that lexicographic
        # order is the order the user sees in the app, siblings included.
        return sorted(items, key=lambda task: str(task.get("position", "")))

    def insert_task(self, tasklist_id: str, body: dict) -> dict:
        return self.request("POST", f"/lists/{tasklist_id}/tasks", json=body)

    def patch_task(self, tasklist_id: str, task_id: str, body: dict) -> dict:
        # PATCH rather than PUT: a full update would clear every field the
        # caller did not happen to send, which for an edit that only changes a
        # due date would silently delete everything else on the task.
        return self.request("PATCH", f"/lists/{tasklist_id}/tasks/{task_id}", json=body)

    def move_task(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        previous: str = "",
        destination_tasklist: str = "",
    ) -> dict:
        # Unset parameters are omitted rather than sent blank: `previous=` is
        # not "at the top of the list" to this API, it is a malformed request.
        params = {
            key: value
            for key, value in {
                "previous": previous,
                "destinationTasklist": destination_tasklist,
            }.items()
            if value
        }
        return self.request("POST", f"/lists/{tasklist_id}/tasks/{task_id}/move", params=params)

    def delete_task(self, tasklist_id: str, task_id: str) -> None:
        self.request("DELETE", f"/lists/{tasklist_id}/tasks/{task_id}")
