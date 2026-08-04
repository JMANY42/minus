import logging
import os
from datetime import datetime
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = WORKSPACE_ROOT / "logs"


def setup_logging(level=logging.DEBUG):
    log_file = LOGS_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{os.getpid()}.log"

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Filter to suppress noisy external modules even if they log via the root logger.
    class _ModuleSuppressFilter(logging.Filter):
        def __init__(self, blocked):
            super().__init__()
            self.blocked = tuple(blocked)

        def filter(self, record):
            # Match on the record's origin only. Matching the formatted message
            # would drop our own logs whenever a blocked name appears in the
            # payload (e.g. a directory listing containing speech_to_text.py).
            name = getattr(record, "name", "") or ""
            module = getattr(record, "module", "") or ""
            pathname = getattr(record, "pathname", "") or ""

            for b in self.blocked:
                if b and (b in name or b in module or b in pathname):
                    return False

            return True

    # RealtimeSTT is kept out of the console (its own halo spinner already
    # owns that line), but its INFO-level state transitions ("State changed
    # from ... to ...", "voice activity detected", "recording started/
    # stopped") are the only way to diagnose recorder hangs after the fact,
    # so they still go to the file.
    _console_suppress_filter = _ModuleSuppressFilter(["faster_whisper", "phonemizer", "RealtimeSTT", "speech_to_text"])
    _file_suppress_filter = _ModuleSuppressFilter(["faster_whisper", "phonemizer", "speech_to_text"])

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(_file_suppress_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)
    stream_handler.addFilter(_console_suppress_filter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    for logger_name in ("openai", "openai._base_client", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    logging.getLogger("speech_to_text").setLevel(logging.WARNING)

    # RealtimeSTT logs through a logger named "realtimestt" (lowercase) with
    # propagate=False (see audio_recorder.py), so nothing it logs ever
    # reaches our root logger's handlers no matter what level we set here -
    # it only writes to its own internally-attached console handler (WARNING
    # by default). Attaching our file handler directly to that logger is the
    # only way to capture its state transitions ("State changed from ... to
    # ...", "voice activity detected", "recording started/stopped"), which
    # is essential for diagnosing recorder hangs.
    logging.getLogger("realtimestt").addHandler(file_handler)

    # Silence external STT/back-end libraries that are too chatty for normal runs.
    # Keep faster_whisper at WARNING (suppress INFO) and phonemizer at ERROR (suppress WARNING).
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    logging.getLogger("phonemizer").setLevel(logging.ERROR)

    return log_file