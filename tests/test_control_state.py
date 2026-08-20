"""RuntimeState: the phase MINUS reports and how watchers hear about it."""

from __future__ import annotations

import contextlib
import json

from minus.control.instrument import ObservedSpeaker
from minus.control.state import IDLE, SPEAKING, THINKING, RuntimeState


class TestPhase:
    def test_starts_idle(self):
        assert RuntimeState().phase == IDLE

    def test_records_a_change(self):
        state = RuntimeState()
        state.set_phase(THINKING)

        assert state.phase == THINKING

    def test_notifies_subscribers(self):
        state = RuntimeState()
        seen: list[str] = []
        state.subscribe(lambda: seen.append(state.phase))

        state.set_phase(THINKING)
        state.set_phase(SPEAKING)

        assert seen == [THINKING, SPEAKING]

    def test_an_unchanged_phase_notifies_nobody(self):
        """`listening` is re-set twice a second; each one is not an event."""
        state = RuntimeState()
        seen: list[str] = []
        state.subscribe(lambda: seen.append(state.phase))

        state.set_phase(THINKING)
        state.set_phase(THINKING)
        state.set_phase(THINKING)

        assert len(seen) == 1

    def test_a_raising_subscriber_does_not_break_the_assistant(self):
        state = RuntimeState()
        seen: list[str] = []

        def broken():
            raise RuntimeError("the dashboard died")

        state.subscribe(broken)
        state.subscribe(lambda: seen.append(state.phase))

        state.set_phase(SPEAKING)

        assert seen == [SPEAKING]
        assert state.phase == SPEAKING


class TestSnapshot:
    def test_is_json_serializable(self):
        """It goes over a socket; anything unserializable is a runtime failure."""
        state = RuntimeState()
        state.provide("deep", lambda: {"in_flight": False, "question": None})

        assert json.loads(json.dumps(state.snapshot()))["deep"]["in_flight"] is False

    def test_sections_are_read_fresh_every_time(self):
        state = RuntimeState()
        answers = iter(["first", "second"])
        state.provide("conversation", lambda: next(answers))

        assert state.snapshot()["conversation"] == "first"
        assert state.snapshot()["conversation"] == "second"

    def test_a_failing_section_does_not_lose_the_whole_snapshot(self):
        state = RuntimeState()
        state.provide("deep", lambda: 1 / 0)

        snapshot = state.snapshot()

        assert snapshot["deep"] is None
        assert snapshot["phase"] == IDLE

    def test_reports_identity_and_uptime(self):
        snapshot = RuntimeState(pid=4242).snapshot()

        assert snapshot["pid"] == 4242
        assert snapshot["uptime_seconds"] >= 0


class TestObservedSpeaker:
    class Recorder:
        def __init__(self, state) -> None:
            self.state = state
            self.phases: list[str] = []
            self.spoken: list[str] = []

        def token(self) -> int:
            return 9

        def speak(self, text: str, *, token: int | None = None) -> None:
            self.phases.append(self.state.phase)
            self.spoken.append(text)

    def test_reports_speaking_while_it_speaks(self):
        state = RuntimeState()
        inner = self.Recorder(state)

        ObservedSpeaker(inner, state).speak("hello")

        assert inner.phases == [SPEAKING]
        assert state.phase == IDLE

    def test_returns_to_idle_even_when_playback_fails(self):
        state = RuntimeState()

        class Broken:
            def token(self) -> int:
                return 1

            def speak(self, text: str, *, token: int | None = None) -> None:
                raise OSError("the audio device went away")

        with contextlib.suppress(OSError):
            ObservedSpeaker(Broken(), state).speak("hello")

        assert state.phase == IDLE

    def test_passes_the_token_through(self):
        state = RuntimeState()
        inner = self.Recorder(state)
        speaker = ObservedSpeaker(inner, state)

        assert speaker.token() == 9
        speaker.speak("hi", token=3)
        assert inner.spoken == ["hi"]
