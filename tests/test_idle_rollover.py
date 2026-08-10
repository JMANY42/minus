"""Ending a conversation after a silence.

The decision itself -- condense now, or wait -- rather than the timer that
triggers it, which is covered in test_transcript_sources.py.
"""

from __future__ import annotations

import threading
import time
from queue import Queue

from minus.cli import Assistant, deliver_deep_result, end_conversation_when_idle
from minus.core.agent import Conversation
from minus.core.escalation import DeepResult
from minus.core.messages import Message, Transcript
from minus.core.sources import MergedTranscriptSource
from minus.memory.facts.store import SqliteFactStore
from minus.memory.service import MemoryService

from .fakes import FakeChatModel, FakeCompletion, FakeEmbedder, FakeMessage


class FakeThinker:
    def __init__(self, in_flight: bool = False) -> None:
        self.in_flight = in_flight

    def status(self) -> dict:
        return {"in_flight": self.in_flight, "question": None, "elapsed_seconds": None}


class FakeConversation:
    def __init__(self, facts: list[dict] | None = None) -> None:
        self.transcript = Transcript()
        self.facts = facts or []
        self.condensed = 0
        self.started: list[str] = []

    def post_conversation(self) -> list[dict]:
        self.condensed += 1
        return self.facts

    def start_new_conversation(self) -> str:
        self.transcript = Transcript()
        conversation_id = f"conv-{len(self.started) + 1}"
        self.started.append(conversation_id)
        return conversation_id


def drain(source) -> tuple[list[str], threading.Thread]:
    """Iterate `source` on its own thread, so the idle clock actually runs."""
    received: list[str] = []
    thread = threading.Thread(target=lambda: received.extend(source), daemon=True)
    thread.start()
    return received, thread


def build(*, said: bool = True, in_flight: bool = False, facts=None):
    conversation = FakeConversation(facts=facts)
    if said:
        conversation.transcript.append(Message.user("something worth remembering"))

    assistant = Assistant(
        conversation=conversation,
        memory=None,
        thinker=FakeThinker(in_flight=in_flight),
        results=Queue(),
        details=None,
    )
    return assistant, conversation


def test_condenses_and_starts_a_fresh_conversation():
    assistant, conversation = build()

    handled = end_conversation_when_idle(assistant, threading.Lock())

    assert handled is True
    assert conversation.condensed == 1
    assert conversation.started == ["conv-1"]
    assert len(conversation.transcript) == 0


def test_says_nothing_happened_when_nothing_was_said():
    """Otherwise every timeout of a long silence writes another empty file."""
    assistant, conversation = build(said=False)

    handled = end_conversation_when_idle(assistant, threading.Lock())

    assert handled is True  # latched: do not ask again until the user speaks
    assert conversation.condensed == 0
    assert conversation.started == []


def test_waits_while_a_deep_answer_is_still_coming():
    """The answer belongs in the conversation that asked for it."""
    assistant, conversation = build(in_flight=True)

    handled = end_conversation_when_idle(assistant, threading.Lock())

    assert handled is False  # declined: ask again after another interval
    assert conversation.condensed == 0
    assert conversation.started == []


def test_rolls_over_once_the_deep_answer_has_landed():
    assistant, conversation = build(in_flight=True)
    floor = threading.Lock()

    assert end_conversation_when_idle(assistant, floor) is False
    assistant.thinker.in_flight = False

    assert end_conversation_when_idle(assistant, floor) is True
    assert conversation.condensed == 1


class RecentlySpoke:
    """A source that reports the user was just spoken to."""

    idle_timeout = 30.0

    def seconds_since_activity(self) -> float:
        return 0.5


class LongSilent:
    idle_timeout = 30.0

    def seconds_since_activity(self) -> float:
        return 45.0


def test_declines_when_something_was_said_while_waiting_for_the_floor():
    """The rollover decision predates the deep answer it queued up behind."""
    assistant, conversation = build()

    handled = end_conversation_when_idle(assistant, threading.Lock(), RecentlySpoke())

    assert handled is False
    assert conversation.condensed == 0


def test_proceeds_when_the_silence_really_did_last():
    assistant, conversation = build()

    handled = end_conversation_when_idle(assistant, threading.Lock(), LongSilent())

    assert handled is True
    assert conversation.condensed == 1


class RecordingSpeaker:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def token(self) -> int:
        return 1

    def speak(self, text: str, *, token: int | None = None) -> None:
        if self.events is not None:
            self.events.append(f"spoke:{text}")


class RecordingSink:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def publish(self, title: str, detail: str) -> None:
        if self.events is not None:
            self.events.append("published")


def test_a_deep_answer_restarts_the_idle_clock_after_it_is_spoken():
    """Order matters: the clock restarts once the answer has been delivered."""
    events: list[str] = []
    assistant, conversation = build()
    assistant.details = RecordingSink(events)

    deliver_deep_result(
        assistant,
        RecordingSpeaker(events),
        threading.Lock(),
        DeepResult(question="q", spoken="Here is the answer.", detail="long form"),
        lambda: events.append("marked"),
    )

    assert events == ["published", "spoke:Here is the answer.", "marked"]
    # The answer is in the transcript, so a later rollover still condenses it.
    assert [message.content for message in conversation.transcript] == [
        "something worth remembering",
        "Here is the answer.",
    ]


def test_a_late_deep_answer_buys_a_full_silence_to_reply_in():
    """The point of the reset: reply time starts when MINUS stops talking.

    Without it, the wait for the deep model is counted as silence, and the
    conversation ends moments after the answer the user wanted to respond to.
    """
    timeout = 0.3
    assistant, conversation = build()
    assistant.details = RecordingSink()
    floor = threading.Lock()

    source = MergedTranscriptSource(
        None,
        idle_timeout=timeout,
        poll=0.01,
        on_idle=lambda: end_conversation_when_idle(assistant, floor, source),
    )
    _, thread = drain(source)

    # Most of the way through the silence, the answer lands and is spoken.
    time.sleep(timeout * 0.7)
    assert conversation.condensed == 0
    deliver_deep_result(
        assistant,
        RecordingSpeaker(),
        floor,
        DeepResult(question="q", spoken="Here is the answer.", detail="long form"),
        source.mark_activity,
    )

    # Past where the original silence would have expired: still going, because
    # being spoken to restarted the clock.
    time.sleep(timeout * 0.7)
    assert conversation.condensed == 0

    # And it still ends once the *new* silence has actually run its course.
    time.sleep(timeout)
    assert conversation.condensed == 1

    source.close()
    thread.join(2)


def test_a_silence_ends_the_conversation_on_disk(tmp_path):
    """End to end: real source, real conversation, real files. Only the LLM is fake.

    A silence should leave the finished conversation condensed on disk and the
    next utterance recorded in a different file.
    """
    store = SqliteFactStore(tmp_path / "facts.db", embedder=FakeEmbedder())
    model = FakeChatModel(
        [
            FakeCompletion(FakeMessage(content="Hello there.")),
            FakeCompletion(FakeMessage(content="[]")),  # fact extraction finds nothing
            FakeCompletion(FakeMessage(content="Still here.")),
        ]
    )
    memory = MemoryService(
        base_dir=tmp_path / "conversations",
        condensed_base_dir=tmp_path / "condensed",
        model=model,
        store=store,
    )
    conversation = Conversation(model=model, memory=memory)
    assistant = Assistant(
        conversation=conversation,
        memory=memory,
        thinker=FakeThinker(),
        results=Queue(),
        details=None,
    )

    floor = threading.Lock()
    source = MergedTranscriptSource(
        None,
        idle_timeout=0.05,
        poll=0.01,
        on_idle=lambda: end_conversation_when_idle(assistant, floor),
    )

    first_file = memory.file_path
    replies = []

    # The conversation runs on this thread, as it does in production -- the
    # fact store's sqlite connection belongs to whichever thread opened it, and
    # the idle handler fires between yields on the consumer's thread.
    def script():
        source.submit("hello")
        time.sleep(0.4)  # long enough for the silence to end the conversation
        # Closing straight after the submit: the queue is FIFO, so the
        # utterance is answered before the stop is seen. Any pause here longer
        # than the timeout would (correctly) end a second conversation too.
        source.submit("are you there")
        source.close()

    threading.Thread(target=script, daemon=True).start()

    for utterance in source:
        with floor:
            replies.append(conversation.reply(utterance))

    assert replies == ["Hello there.", "Still here."]

    # The finished conversation was condensed...
    condensed = list((tmp_path / "condensed").glob("*.json"))
    assert len(condensed) == 1

    # ...and the second utterance went somewhere new.
    assert memory.file_path != first_file
    conversations = list((tmp_path / "conversations").glob("*.json"))
    assert len(conversations) == 2

    store.close()


def test_holds_the_floor_for_the_whole_rollover():
    """The deep courier appends under this lock; condensing must not race it."""
    assistant, conversation = build()
    floor = threading.Lock()

    done = threading.Event()
    with floor:
        thread = threading.Thread(
            target=lambda: (end_conversation_when_idle(assistant, floor), done.set()),
            daemon=True,
        )
        thread.start()
        # Nothing may happen while another producer holds the floor.
        assert not done.wait(0.2)
        assert conversation.condensed == 0

    assert done.wait(2)
    assert conversation.condensed == 1
