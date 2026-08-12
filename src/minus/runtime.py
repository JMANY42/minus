"""One conversation, running.

The loop the assistant actually spends its life in, plus the three things that
share the floor with it: the courier that delivers escalated answers, the idle
rollover that ends a conversation after a silence, and the SIGTERM handler that
makes `systemctl stop` end one properly rather than discard it.

Separate from the composition root because this is behaviour rather than
wiring. It receives everything it touches -- the assistant, the speaker, the
lock -- and constructs none of it, which is what lets the tests drive a whole
conversation with fakes and no entry point in sight.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
from dataclasses import dataclass
from queue import Queue

from minus.control.state import LISTENING, THINKING, RuntimeState
from minus.core.agent import Conversation
from minus.core.escalation import DeepThinker
from minus.core.messages import Message
from minus.core.protocols import DetailSink
from minus.memory.service import MemoryService
from minus.services.json import pretty_json

# Ends the courier thread. A sentinel rather than a flag because the courier
# is blocked in Queue.get() and needs something to arrive to wake it.
_STOP = object()

logger = logging.getLogger(__name__)


@dataclass
class Assistant:
    """The wired object graph one conversation runs on."""

    conversation: Conversation
    memory: MemoryService
    thinker: DeepThinker
    results: Queue
    details: DetailSink


@contextlib.contextmanager
def end_conversation_on_sigterm(source):
    """Make `systemctl stop` end the conversation rather than discard it.

    systemd sends SIGTERM, which Python's default handler turns into an
    immediate exit -- so the work in `conversation_loop`'s `finally`, the
    condensation and fact extraction that the whole session's learning depends
    on, never ran. Closing the source instead ends the loop the same way an
    exit phrase does, and the polling get() in MergedTranscriptSource is what
    guarantees the flag is noticed promptly.

    Only on the main thread, because CPython permits signal handlers nowhere
    else, and restored afterwards so this composes with barge_in_on_sigint.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = signal.signal(signal.SIGTERM, lambda *_: source.close())
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def deliver_deep_result(assistant, speaker, floor, result, mark_activity=None) -> None:
    """Publish one escalated answer's two channels."""
    # Detail first, and outside the lock: it is file I/O with nothing to
    # serialize against, and the spoken line may refer to the write-up.
    assistant.details.publish(result.question, result.detail)

    with floor:
        # Captured here rather than when the escalation started. The user has
        # almost certainly spoken during the seconds the deep tier was running,
        # which would make an escalation-time token stale and silently drop
        # every single deep answer.
        token = speaker.token()
        assistant.conversation.transcript.append(Message(role="assistant", content=result.spoken))
        logger.info("Deep answer:\n%s", pretty_json(result.spoken))
        speaker.speak(result.spoken, token=token)

        # After speaking, and still holding the floor. A deep answer arrives
        # long after the question, so by now the idle clock has been running
        # through the whole wait -- and the user has just been handed something
        # to respond to. Restarting it here gives them the full silence to
        # answer in, and an idle rollover already queued behind this lock sees
        # the fresh clock rather than condensing on top of the answer.
        if mark_activity is not None:
            mark_activity()


def deep_result_courier(assistant, speaker, floor, mark_activity=None) -> None:
    """Deliver escalated answers as they land, until told to stop."""
    while True:
        result = assistant.results.get()
        if result is _STOP:
            return
        try:
            deliver_deep_result(assistant, speaker, floor, result, mark_activity)
        except Exception:
            # This thread is the only thing delivering deep answers; letting it
            # die over one bad result would silently disable escalation for the
            # rest of the session.
            logger.exception("Failed to deliver a deep answer")


def end_conversation_when_idle(assistant: Assistant, floor: threading.Lock, source=None) -> bool:
    """End the current conversation after a silence and open a fresh one.

    Returns False to decline, which leaves the idle timer armed for another
    interval; True means "done, do not ask again until the user says
    something".
    """
    conversation = assistant.conversation

    with floor:
        # Re-checked under the lock, because acquiring it may have meant
        # waiting for the courier to finish speaking a deep answer. The
        # decision to roll over was made before that answer existed.
        if source is not None and source.seconds_since_activity() < source.idle_timeout:
            return False

        if not conversation.transcript:
            # Nothing was said. Condensing would write an empty file, and would
            # do it again on every timeout for as long as the silence lasted.
            return True

        if assistant.thinker.status()["in_flight"]:
            # An escalation outlives a half-minute silence easily --
            # deep_timeout_seconds defaults to 120. Rolling over now would
            # condense a conversation that is missing its own answer, and then
            # deliver that answer into a fresh, unrelated one.
            logger.debug("Idle, but a deep answer is still coming; leaving the conversation open.")
            return False

        facts = conversation.post_conversation()
        conversation_id = conversation.start_new_conversation()

    logger.info("Idle; conversation ended. Now recording to %s", conversation_id)
    if facts:
        logger.info("Facts extracted from the finished conversation:\n%s", pretty_json(facts))
    return True


def end_conversation_now(assistant: Assistant, floor: threading.Lock, source=None) -> dict:
    """End the current conversation because somebody asked, and open a fresh one.

    The deliberate twin of `end_conversation_when_idle`, and deliberately
    without its refusals. Those exist because a silence is only a guess that
    the conversation is over -- an empty transcript or a deep answer still
    coming means the guess was wrong. A request is not a guess, so the only
    thing kept from that path is the floor: condensing while a reply is being
    spoken would read a transcript that is still being written.

    A deep answer still in flight will be delivered into the new conversation
    rather than the one it was asked in. Logged rather than refused, since
    holding the old conversation open would ignore what was actually asked for.

    Slow -- condensing and fact extraction are two model calls -- so the caller
    is responsible for not running this anywhere that something is waiting on
    an answer.
    """
    conversation = assistant.conversation

    with floor:
        if not conversation.transcript:
            # Nothing to condense, and nothing to roll over to: the conversation
            # this would open is the one already open. Saying so is better than
            # leaving another empty file behind on every press.
            logger.info("Asked to end an empty conversation; it is already a fresh one.")
            return {"conversation_id": assistant.memory.conversation_id, "facts": 0}

        if assistant.thinker.status()["in_flight"]:
            logger.info(
                "Ending the conversation with a deep answer still coming; "
                "it will be delivered into the next one."
            )

        facts = conversation.post_conversation()
        conversation_id = conversation.start_new_conversation()

    # The idle clock has been running through whatever silence led to this, and
    # the fresh conversation should not inherit it and roll over immediately.
    if source is not None:
        source.mark_activity()

    logger.info("Conversation ended on request. Now recording to %s", conversation_id)
    if facts:
        logger.info("Facts extracted from the finished conversation:\n%s", pretty_json(facts))
    return {"conversation_id": conversation_id, "facts": len(facts)}


def conversation_loop(
    transcripts, assistant, speaker, floor, mark_activity=None, state=None
) -> None:
    """Drive one conversation to completion.

    The post-conversation work runs in a `finally` so that quitting with Ctrl-C
    still condenses the transcript and extracts durable facts. It previously sat
    after the loop, so an interrupt discarded everything the session had learned.

    Three producers share one speaker and one transcript: this loop, the courier
    thread carrying escalated answers, and the idle rollover. `floor` is what
    stops a deep answer from being spoken over a live reply, and stops any two
    of them from touching the transcript at once. It is built by the caller,
    since all three need the same one.
    """
    conversation = assistant.conversation
    state = state if state is not None else RuntimeState()
    courier = threading.Thread(
        target=deep_result_courier,
        args=(assistant, speaker, floor, mark_activity),
        name="deep-courier",
        daemon=True,
    )
    courier.start()

    try:
        state.set_phase(LISTENING)
        for transcript in transcripts:
            logger.info("Transcript received:\n%s", pretty_json(transcript))
            state.set_phase(THINKING)

            with floor:
                # Captured before generation starts: if the user begins talking
                # while the model is still thinking, this token goes stale and
                # the reply is dropped rather than spoken over them.
                token = speaker.token()

                response = conversation.reply(transcript)
                logger.info("Assistant response:\n%s", pretty_json(response))
                # Speaking and idle are reported by ObservedSpeaker, which
                # wraps this one -- the courier speaks too, and instrumenting
                # the speaker covers both without a second copy here.
                speaker.speak(response, token=token)

            state.set_phase(LISTENING)
    except KeyboardInterrupt:
        logger.info("Interrupted; wrapping up the conversation.")
    finally:
        assistant.results.put(_STOP)
        courier.join(timeout=2.0)

        conversation.post_conversation()
        facts = conversation.memory.all_facts()
        if facts:
            logger.info("Semantic memory facts:\n%s", pretty_json(facts))
        else:
            logger.info("No semantic memory stored.")
