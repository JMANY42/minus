"""Enough of Google's Tasks and Calendar APIs to be wrong against.

Served through a real `httpx.MockTransport`, so the tests exercise the actual
query parameters and request bodies rather than asserting that a stub was
called. Shared by both test modules because both go through one grant, and the
token endpoint is the same endpoint.

Dates are relative to today rather than written down. A fixture pinned to 2026
would quietly stop covering anything once it fell outside the year-wide window
the event lookup searches, and it would fail as a date rather than as a bug.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

TZ = "America/Chicago"


def today():
    return datetime.now(ZoneInfo(TZ)).date()


def day(offset: int) -> str:
    return (today() + timedelta(days=offset)).isoformat()


def _task(task_id: str, title: str, *, position: str = "0", status: str = "needsAction") -> dict:
    return {"id": task_id, "title": title, "position": position, "status": status}


def _timed(event_id: str, summary: str, offset: int, start: str, end: str, **extra) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": f"{day(offset)}T{start}:00-05:00", "timeZone": TZ},
        "end": {"dateTime": f"{day(offset)}T{end}:00-05:00", "timeZone": TZ},
        **extra,
    }


def _all_day(event_id: str, summary: str, first: int, last_exclusive: int, **extra) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"date": day(first)},
        "end": {"date": day(last_exclusive)},
        **extra,
    }


class FakeGoogle:
    """One account: two task lists, three calendars, and a token endpoint."""

    def __init__(self) -> None:
        self.lists = [
            {"id": "list-1", "title": "My Tasks"},
            {"id": "list-2", "title": "Weekend"},
        ]
        self.tasks = {
            "list-1": [
                _task("t1", "Buy milk", position="0"),
                _task("t2", "Call mum", position="1"),
                _task("t3", "Call mum's dentist", position="2"),
                _task("t4", "Old thing", position="3", status="completed"),
            ],
            "list-2": [_task("t5", "Wash the car", position="0")],
        }

        # Two writable and one subscribed read-only, which is what a real
        # account looks like and what the writable filter exists for.
        self.calendars = [
            {
                "id": "me@example.com",
                "summary": "Personal",
                "primary": True,
                "accessRole": "owner",
            },
            {"id": "work@group.calendar.google.com", "summary": "Work", "accessRole": "writer"},
            {
                "id": "holidays@group.v.calendar.google.com",
                "summary": "Holidays in the US",
                "accessRole": "reader",
            },
        ]
        self.events = {
            "me@example.com": [_timed("ev1", "Dentist", 5, "14:00", "15:00", location="Main St")],
            "work@group.calendar.google.com": [
                _timed("ev2", "Standup", 1, "09:30", "09:45"),
                _all_day("ev3", "Conference", 12, 15),
            ],
            "holidays@group.v.calendar.google.com": [_all_day("ev4", "Labor Day", 20, 21)],
        }

        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        # Number of API calls to answer with a 401 before the token works.
        self.reject_tokens = 0
        # Set to a Google error payload to fail the next API call with a 403.
        self.forbid: dict | None = None

    # ---- Transport ----

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urllib.parse.unquote(request.url.path)

        if path == "/token":
            return self._token(request)
        if self.reject_tokens > 0:
            self.reject_tokens -= 1
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid"}})
        if self.forbid is not None:
            payload, self.forbid = self.forbid, None
            return httpx.Response(403, json=payload)

        if path.startswith("/tasks/v1/"):
            return self._tasks_api(request, path.removeprefix("/tasks/v1/"))
        if path.startswith("/calendar/v3/"):
            return self._calendar_api(request, path.removeprefix("/calendar/v3/"))
        return httpx.Response(404, json={"error": {"code": 404, "message": f"No route {path}"}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        form = dict(part.split("=", 1) for part in request.content.decode().split("&"))
        if form.get("refresh_token") == "revoked":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Token has been expired"}
            )
        return httpx.Response(
            200, json={"access_token": f"token-{self.token_calls}", "expires_in": 3600}
        )

    # ---- Tasks ----

    def _tasks_api(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "users/@me/lists":
            return httpx.Response(200, json={"items": self.lists})

        parts = path.removeprefix("lists/").split("/")
        tasklist_id = parts[0]
        if tasklist_id not in self.tasks:
            return httpx.Response(404, json={"error": {"code": 404, "message": "No such list"}})

        if len(parts) == 2:
            if request.method == "GET":
                show_completed = request.url.params.get("showCompleted") in ("true", "True")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            task
                            for task in self.tasks[tasklist_id]
                            if show_completed or task["status"] != "completed"
                        ]
                    },
                )
            body = json.loads(request.content)
            created = _task(f"new-{len(self.requests)}", body.get("title", ""), position="9")
            created.update({key: value for key, value in body.items() if value is not None})
            self.tasks[tasklist_id].append(created)
            return httpx.Response(200, json=created)

        found = next((t for t in self.tasks[tasklist_id] if t["id"] == parts[2]), None)
        if found is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": "No such task"}})

        if request.method == "DELETE":
            self.tasks[tasklist_id].remove(found)
            return httpx.Response(204)
        if request.method == "PATCH":
            for key, value in json.loads(request.content).items():
                if value is None:
                    found.pop(key, None)
                else:
                    found[key] = value
            return httpx.Response(200, json=found)

        destination = request.url.params.get("destinationTasklist")
        if destination:
            self.tasks[tasklist_id].remove(found)
            self.tasks[destination].append(found)
        return httpx.Response(200, json=found)

    # ---- Calendar ----

    def _calendar_api(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "users/me/calendarList":
            return httpx.Response(200, json={"items": self.calendars})

        parts = path.removeprefix("calendars/").split("/")
        calendar_id = parts[0]
        if calendar_id not in self.events:
            return httpx.Response(404, json={"error": {"code": 404, "message": "No such calendar"}})

        if len(parts) == 2:
            if request.method == "GET":
                return httpx.Response(200, json={"items": self._in_window(calendar_id, request)})
            body = json.loads(request.content)
            created = {"id": f"new-{len(self.requests)}", **body}
            self.events[calendar_id].append(created)
            return httpx.Response(200, json=created)

        found = next((e for e in self.events[calendar_id] if e["id"] == parts[2]), None)
        if found is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": "No such event"}})

        if request.method == "DELETE":
            self.events[calendar_id].remove(found)
            return httpx.Response(204)
        if request.method == "PATCH":
            found.update(json.loads(request.content))
            return httpx.Response(200, json=found)

        destination = request.url.params.get("destination")
        self.events[calendar_id].remove(found)
        self.events[destination].append(found)
        return httpx.Response(200, json=found)

    def _in_window(self, calendar_id: str, request: httpx.Request) -> list[dict]:
        """Only the events the requested window covers.

        Crude -- it compares the date halves as strings -- but enough that a
        tool computing the wrong window fails here rather than passing on a
        fake that returns everything regardless.
        """
        low = (request.url.params.get("timeMin") or "")[:10]
        high = (request.url.params.get("timeMax") or "")[:10]
        return [event for event in self.events[calendar_id] if low <= _starts_on(event) < high]


def _starts_on(event: dict) -> str:
    start = event["start"]
    return (start.get("date") or start.get("dateTime", ""))[:10]
