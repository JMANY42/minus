"""Decorators that report what a collaborator is doing.

A decorator rather than edits at the call sites. Two different threads speak
-- the conversation loop and the deep courier -- so instrumenting `speak()`
where it is called means two copies of the same three lines, and a third the
next time something learns to talk. Wrapping the speaker once covers every
caller, present and future, and keeps the dependency pointing the right way:
`tts.py` still knows nothing about the control channel.
"""

from __future__ import annotations

from typing import Any

from minus.control.state import IDLE, SPEAKING


class ObservedSpeaker:
    """A SpeechSynthesizer that reports when it is talking.

    Restores `idle` rather than the previous phase because there is no useful
    previous phase to return to: whatever was happening before, once the
    speaking stops the assistant is between turns, and the conversation loop
    sets `listening` as soon as it goes back to waiting.
    """

    def __init__(self, inner: Any, state: Any) -> None:
        self._inner = inner
        self._state = state

    def token(self) -> int:
        return self._inner.token()

    def speak(self, text: str, *, token: int | None = None) -> None:
        self._state.set_phase(SPEAKING)
        try:
            self._inner.speak(text, token=token)
        finally:
            self._state.set_phase(IDLE)

    def __getattr__(self, name: str) -> Any:
        # Anything else the speaker offers passes straight through, so this
        # stays a decorator rather than a reimplementation that has to be kept
        # in step with KokoroSpeaker.
        return getattr(self._inner, name)
