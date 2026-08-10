"""Ending a conversation after a silence.

The decision itself -- condense now, or wait -- rather than the timer that
triggers it, which is covered in test_transcript_sources.py.
"""

from __future__ import annotations

import threading
import time
from queue import Queue

from minus.cli import Assistant, end_conversation_when_idle
from minus.core.agent import Conversation
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
