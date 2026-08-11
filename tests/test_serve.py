"""Headless operation: the unit file, the parser, and the SIGTERM handler."""

from __future__ import annotations

import os
import signal
import threading
import time

from minus.cli import build_parser
from minus.control.systemd import install_hint, render_unit
from minus.core.sources import MergedTranscriptSource
from minus.runtime import end_conversation_on_sigterm


class TestUnitFile:
    def test_substitutes_both_paths(self):
        unit = render_unit("/opt/venv/bin/minus", "/srv/minus")

        assert "ExecStart=/opt/venv/bin/minus serve" in unit
        assert "WorkingDirectory=/srv/minus" in unit
        assert "Environment=MINUS_PROJECT_ROOT=/srv/minus" in unit

    def test_leaves_no_placeholders_behind(self):
        unit = render_unit("/opt/venv/bin/minus", "/srv/minus")

        assert "{" not in unit
        # systemd's own %h/%i specifiers would resolve to the wrong thing here,
        # since the point of generating this is that the paths are already known.
        assert "%h" not in unit

    def test_stops_with_sigterm_and_waits_for_the_wrap_up(self):
        """A graceful stop condenses and extracts; cutting it short loses both."""
        unit = render_unit("/opt/venv/bin/minus", "/srv/minus")

        assert "KillSignal=SIGTERM" in unit
        assert "TimeoutStopSec=45" in unit

    def test_is_a_user_unit_that_wants_the_audio_stack(self):
        unit = render_unit("/opt/venv/bin/minus", "/srv/minus")

        assert "WantedBy=default.target" in unit
        assert "pipewire" in unit

    def test_the_hint_mentions_lingering(self):
        """Without enable-linger the service dies at logout, which defeats it."""
        assert "enable-linger" in install_hint("/tmp/minus.service")


class TestParser:
    def test_serve_is_a_subcommand(self):
        assert build_parser().parse_args(["serve"]).command == "serve"

    def test_serve_defaults_to_the_microphone(self):
        assert build_parser().parse_args(["serve"]).no_mic is False

    def test_serve_can_be_socket_only(self):
        assert build_parser().parse_args(["serve", "--no-mic"]).no_mic is True

    def test_the_root_flag_still_reaches_serve(self):
        """The subparser must not overwrite it with its own default."""
        assert build_parser().parse_args(["--no-mic", "serve"]).no_mic is True

    def test_systemd_unit_is_a_subcommand(self):
        assert build_parser().parse_args(["systemd-unit"]).command == "systemd-unit"


class TestSigterm:
    def test_ends_the_conversation_instead_of_killing_the_process(self):
        """The default handler exits immediately, discarding the session."""
        source = MergedTranscriptSource(None, idle_timeout=0, poll=0.01)
        ended = threading.Event()
        threading.Thread(target=lambda: (list(source), ended.set()), daemon=True).start()
        time.sleep(0.05)

        with end_conversation_on_sigterm(source):
            os.kill(os.getpid(), signal.SIGTERM)
            assert ended.wait(2), "SIGTERM did not end the transcript source"

    def test_the_previous_handler_is_restored(self):
        source = MergedTranscriptSource(None, idle_timeout=0)
        before = signal.getsignal(signal.SIGTERM)

        with end_conversation_on_sigterm(source):
            assert signal.getsignal(signal.SIGTERM) is not before

        assert signal.getsignal(signal.SIGTERM) is before

    def test_does_nothing_off_the_main_thread(self):
        """signal.signal() raises anywhere else; the context must still work."""
        source = MergedTranscriptSource(None, idle_timeout=0)
        worked = threading.Event()

        def run() -> None:
            with end_conversation_on_sigterm(source):
                worked.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2)

        assert worked.is_set()
