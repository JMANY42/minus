"""A SpeechSynthesizer that prints instead of speaking.

This exists so the assistant is usable on a box with no PortAudio, no sound card
and no Kokoro weights: `minus --no-mic --no-audio`. It is a deliberate mode, not
a fallback — the CLI only selects it when asked, so a broken audio stack still
fails where it should instead of silently going quiet.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from minus.audio.interrupt import InterruptBus


class ConsoleSpeaker:
    """Writes replies to stdout, honouring the same interrupt token as speech."""

    def __init__(
        self,
        interrupts: InterruptBus,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.interrupts = interrupts
        self.echo = echo

    def token(self) -> int:
        return self.interrupts.token()

    def speak(self, text: str, *, token: int | None = None) -> None:
        # Same rule as the speaker: a reply whose token went stale while the
        # model was generating is dropped rather than printed over the user.
        if self.interrupts.is_stale(token):
            return
        self.echo(f"MINUS: {text}")

    def __getattr__(self, name: str) -> Any:
        # Live TTS settings are applied to the speaker object by the config
        # controller; on this one they are attributes with no effect.
        raise AttributeError(name)
