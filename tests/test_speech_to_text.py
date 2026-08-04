import builtins
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

import minus.audio.stt as speech_to_text_module


class CliTranscriptTests(unittest.TestCase):
    def test_cli_input_requests_interrupt_before_yielding(self):
        with patch.object(builtins, "input", side_effect=["hello there", EOFError()]):
            with patch.object(speech_to_text_module, "request_interrupt") as mock_interrupt:
                transcripts = speech_to_text_module.iter_cli_transcripts()

                self.assertEqual(next(transcripts), "hello there")
                self.assertEqual(mock_interrupt.call_count, 1)

                with self.assertRaises(StopIteration):
                    next(transcripts)


if __name__ == "__main__":
    unittest.main()
