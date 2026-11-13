"""The console speaker: real stdout, real interrupt token."""

from __future__ import annotations

from minus.audio.console import ConsoleSpeaker
from minus.audio.interrupt import InterruptBus


def test_it_prints_the_reply(capsys):
    speaker = ConsoleSpeaker(InterruptBus())
    speaker.speak("hello there")
    assert capsys.readouterr().out == "MINUS: hello there\n"


def test_a_stale_token_drops_the_reply(capsys):
    bus = InterruptBus()
    speaker = ConsoleSpeaker(bus)
    token = speaker.token()
    bus.request()
    speaker.speak("too late", token=token)
    assert capsys.readouterr().out == ""


def test_a_none_token_is_never_stale(capsys):
    speaker = ConsoleSpeaker(InterruptBus())
    speaker.speak("fine", token=None)
    assert "fine" in capsys.readouterr().out
