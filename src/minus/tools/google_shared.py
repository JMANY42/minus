"""What the tasks and calendar tools both need: certainty, and dates.

Both families have the same shape. There is a *container* the user has to be
sure about (a task list, a calendar), an *item* referred to by name rather than
by id, and a *date* that may or may not carry a time. Writing that twice would
have meant two subtly different ideas of what counts as ambiguous, which is the
one thing here that must not drift.

The rule these encode, in one sentence: **never invent a field the user is
entitled to have chosen.** Three cases come out of it, and they are deliberately
answered differently, because they need different things from different people:

  * *Ambiguous* -- two tasks called "call mum", or four calendars and no name.
    Only the user can settle it, so this raises `ClarificationNeeded` and the
    loop turns it into a question. It is never resolved by picking the first.
  * *Not found* -- no task by that name anywhere. Retrying cannot help and the
    user has nothing to choose between, so this is an ordinary
    `ToolArgumentError` carrying the real names, which the model reports.
  * *Unambiguous* -- one calendar on the account, or a list named in `.env`.
    Using it is not a guess: there was nothing to choose. This is why the rule
    is not simply "always ask", which would make MINUS insufferable for the
    single-list account that most people have.

A field the user may legitimately have said "there isn't one" about -- a task
with no due date, an event with no location -- is not expressible as an empty
string, because empty is exactly what an unsure model sends. It is spelled with
the word `none`, so that "the user told me there is no location" and "I did not
ask about the location" stop being the same value.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from datetime import time as Time
from typing import Any
from zoneinfo import ZoneInfo

from minus.errors import ClarificationNeeded, ToolArgumentError

logger = logging.getLogger(__name__)

# The word that means "the user said there is not one", as opposed to an empty
# string, which means "nobody has said". See the module docstring.
NONE_WORDS = frozenset({"none", "no", "nothing", "n/a", "na", "unset", "no due date"})

# How many names an error message lists before it stops being speakable.
_MAX_NAMED = 12


def said_none(value: str) -> bool:
    """Whether the model is reporting an absence rather than failing to fill one in."""
    return value.strip().casefold() in NONE_WORDS


def require(value: str, question: str) -> str:
    """The value, or the question to ask about it.

    Blank means the model left the field empty, which for a field in this set
    means it did not know -- so the answer is the question, not a default.
    """
    if not value.strip():
        raise ClarificationNeeded(question)
    return value.strip()


# ---- Matching a name to a thing ----


def _rank(item: dict, reference: str, title_key: str) -> int | None:
    """How well `reference` names `item`: lower is better, None is not at all.

    Ranked rather than filtered so that an exact title always beats a
    substring. Without that, a task called "shopping" would be ambiguous the
    moment a "shopping list" existed beside it, and would stay unaddressable
    however precisely the user named it.
    """
    title = str(item.get(title_key, ""))
    wanted = reference.strip()
    folded = wanted.casefold()

    if item.get("id") == wanted:
        return 0
    if title == wanted:
        return 1
    if title.casefold() == folded:
        return 2
    if folded and folded in title.casefold():
        return 3
    return None


def _names(items: Iterable[Any], title_key: str) -> str:
    titles = [f"{item.get(title_key, '')!r}" for item in items][:_MAX_NAMED]
    return ", ".join(titles) or "none"


def match_one(
    items: Sequence[dict], reference: str, *, kind: str, title_key: str = "title"
) -> dict:
    """The one item `reference` names, or the reason it is not one."""
    ranked = [
        (rank, item) for item in items if (rank := _rank(item, reference, title_key)) is not None
    ]
    if not ranked:
        raise ToolArgumentError(
            f"There is no {kind} called {reference!r}. The ones that exist are: "
            f"{_names(items, title_key)}."
        )

    best = min(rank for rank, _ in ranked)
    candidates = [item for rank, item in ranked if rank == best]
    if len(candidates) > 1:
        raise ClarificationNeeded(
            f"More than one {kind} matches {reference!r}: {_names(candidates, title_key)}. "
            "Which one did they mean?"
        )
    return candidates[0]


def choose_container(
    containers: Sequence[dict],
    named: str,
    configured: str,
    *,
    kind: str,
    setting: str,
    title_key: str = "title",
) -> dict:
    """Which list or calendar to act on, without ever picking one arbitrarily.

    In order: what the user named, then what `.env` names, then the only one
    there is. If none of those settle it, the user is asked -- because at that
    point the answer exists only in their head.
    """
    if named.strip():
        return match_one(containers, named, kind=kind, title_key=title_key)

    if configured.strip():
        # A stated preference, not a guess. A name that no longer exists is a
        # configuration error and is reported as one: falling back silently
        # would put tasks somewhere the user believes they configured away.
        try:
            return match_one(containers, configured, kind=kind, title_key=title_key)
        except (ClarificationNeeded, ToolArgumentError) as exc:
            raise ToolArgumentError(
                f"{setting} in .env names {configured!r}, which does not match one {kind}: {exc}"
            ) from exc

    if len(containers) == 1:
        # Nothing to choose between, so nothing to ask about.
        return containers[0]

    raise ClarificationNeeded(
        f"Which {kind} should this go on? The options are {_names(containers, title_key)}."
    )


def locate(
    containers: Sequence[dict],
    items_of: Callable[[dict], Sequence[dict]],
    reference: str,
    *,
    kind: str,
    title_key: str = "title",
    container_title_key: str = "title",
) -> tuple[dict, dict]:
    """Find one named item across every container, and say which it was on.

    For editing, moving and deleting, where the user names the thing but rarely
    the list it lives on. Searching all of them and finding exactly one is not
    a guess -- it is the answer -- and it avoids asking "which list?" about a
    task whose name occurs once on the whole account. Two matches is a genuine
    ambiguity and is asked about, naming the containers rather than repeating
    the same title twice.
    """
    hits: list[tuple[int, dict, dict]] = []
    for container in containers:
        for item in items_of(container):
            rank = _rank(item, reference, title_key)
            if rank is not None:
                hits.append((rank, container, item))

    if not hits:
        raise ToolArgumentError(
            f"There is no {kind} called {reference!r} on {_names(containers, container_title_key)}."
        )

    best = min(rank for rank, _, _ in hits)
    candidates = [(container, item) for rank, container, item in hits if rank == best]
    if len(candidates) > 1:
        where = ", ".join(
            f"{item.get(title_key, '')!r} on {container.get(container_title_key, '')!r}"
            for container, item in candidates[:_MAX_NAMED]
        )
        raise ClarificationNeeded(
            f"More than one {kind} matches {reference!r}: {where}. Which one?"
        )
    return candidates[0]


# ---- Dates and times ----


@dataclass(frozen=True)
class When:
    """A moment the user named, which may or may not have a time in it.

    `time is None` is the whole all-day/timed distinction, carried in the value
    rather than in a separate flag beside it. That is deliberate: a flag and a
    value can contradict each other, and when they did there would be no way to
    tell which half was the guess.
    """

    date: Date
    time: Time | None = None

    @property
    def all_day(self) -> bool:
        return self.time is None

    def iso(self) -> str:
        """RFC 3339 without a zone, which is what Google pairs with `timeZone`."""
        if self.time is None:
            return self.date.isoformat()
        return datetime.combine(self.date, self.time).isoformat(timespec="seconds")


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M%p", "%I%p")
_RELATIVE_DAYS = {"today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1}


def today_in(timezone: str) -> Date:
    """The current date where the user is, not where the server thinks it is.

    In America/Chicago an evening "remind me tomorrow" resolved in UTC lands on
    the day after that for six hours every night.
    """
    return datetime.now(ZoneInfo(timezone)).date()


def parse_when(text: str, timezone: str, *, field: str = "date") -> When:
    """A spoken or written date, with a time if one was given.

    Accepts `2026-08-25`, `2026-08-25 14:00`, `2026-08-25T14:00:00`, `2pm`
    attached to either, and `today`/`tomorrow` with or without a time. What it
    will not do is invent the missing half: a date with no time comes back as
    all-day, and it is the caller's job to decide whether that was what the
    user meant.
    """
    raw = text.strip()
    if not raw:
        raise ClarificationNeeded(f"What {field} should this be?")

    # A trailing zone is dropped rather than honoured. Everything here is in
    # the assistant's configured timezone, and a model that appended a `Z` it
    # was never told is asserting UTC it has no basis for.
    cleaned = raw.removesuffix("Z").removesuffix("z")
    lowered = cleaned.lower().replace("t", " ", 1) if _looks_iso(cleaned) else cleaned.lower()
    parts = lowered.split()

    if parts and parts[0] in _RELATIVE_DAYS:
        day = today_in(timezone) + timedelta(days=_RELATIVE_DAYS[parts[0]])
        rest = " ".join(parts[1:])
        # "tonight" without a time is not an all-day event, and it is not 00:00
        # either -- it is a time nobody has stated.
        if parts[0] == "tonight" and not rest:
            raise ClarificationNeeded(f"What time {field} did they mean by 'tonight'?")
        return When(day, _parse_time(rest, field) if rest else None)

    if not parts:
        raise ClarificationNeeded(f"What {field} should this be?")

    day = _parse_date(parts[0], field)
    rest = " ".join(parts[1:])
    return When(day, _parse_time(rest, field) if rest else None)


def _looks_iso(text: str) -> bool:
    return len(text) > 10 and text[4] == "-" and text[10] in "Tt"


def _parse_date(text: str, field: str) -> Date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ToolArgumentError(
        f"Could not read {text!r} as a {field}. Use YYYY-MM-DD, 'today' or 'tomorrow'."
    )


def _parse_time(text: str, field: str) -> Time:
    compact = text.replace(" ", "").upper()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(compact, fmt).time()
        except ValueError:
            continue
    raise ToolArgumentError(f"Could not read {text!r} as a time for the {field}. Use 14:00 or 2pm.")


__all__ = [
    "NONE_WORDS",
    "When",
    "choose_container",
    "locate",
    "match_one",
    "parse_when",
    "require",
    "said_none",
    "today_in",
]
