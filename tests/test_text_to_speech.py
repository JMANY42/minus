import signal
import sys
import types
import unittest
from unittest.mock import patch

fake_kokoro = types.ModuleType("kokoro_onnx")


class _FakeKokoro:
    def __init__(self, *args, **kwargs):
        pass


fake_kokoro.Kokoro = _FakeKokoro
fake_sounddevice = types.ModuleType("sounddevice")
fake_sounddevice.play = lambda *args, **kwargs: None
fake_sounddevice.wait = lambda *args, **kwargs: None
fake_sounddevice.stop = lambda *args, **kwargs: None

sys.modules["kokoro_onnx"] = fake_kokoro
sys.modules["sounddevice"] = fake_sounddevice

import minus.audio.tts as text_to_speech_module


class SpeakInterruptTests(unittest.TestCase):
    def test_ctrl_c_during_playback_stops_audio_without_exiting(self):
        previous_handler = object()
        installed_handlers = []
        wait_calls = {"count": 0}
        synthesized_chunks = [([1.0], 22050), ([2.0], 22050)]

        def fake_signal(signum, handler):
            installed_handlers.append(handler)
            return previous_handler

        def fake_wait():
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                installed_handlers[0](signal.SIGINT, None)

        with patch.object(text_to_speech_module, "_split_text_into_chunks", return_value=["hello", "world"]), \
            patch.object(text_to_speech_module, "_synthesize_chunk", side_effect=synthesized_chunks), \
            patch.object(text_to_speech_module.signal, "signal", side_effect=fake_signal), \
            patch.object(text_to_speech_module.signal, "getsignal", return_value=previous_handler), \
            patch.object(text_to_speech_module.sd, "play") as mock_play, \
            patch.object(text_to_speech_module.sd, "wait", side_effect=fake_wait), \
            patch.object(text_to_speech_module.sd, "stop") as mock_stop:
            text_to_speech_module.speak("hello world")

        self.assertEqual(mock_play.call_count, 1)
        self.assertEqual(mock_stop.call_count, 1)
        self.assertEqual(wait_calls["count"], 1)
        self.assertEqual(len(installed_handlers), 2)
        self.assertIs(installed_handlers[1], previous_handler)
