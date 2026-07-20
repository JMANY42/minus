import json
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
            name = getattr(record, "name", "") or ""
            module = getattr(record, "module", "") or ""
            pathname = getattr(record, "pathname", "") or ""
            try:
                msg = record.getMessage() or ""
            except Exception:
                msg = ""

            for b in self.blocked:
                if b and (b in name or b in module or b in pathname or b in msg):
                    return False

            return True

    _suppress_filter = _ModuleSuppressFilter(["faster_whisper", "phonemizer", "RealtimeSTT", "speech_to_text"])

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(_suppress_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)
    stream_handler.addFilter(_suppress_filter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    for logger_name in ("groq", "groq._base_client", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Suppress verbose STT module logs so only warnings and above are emitted from STT.
    for stt_logger in ("RealtimeSTT", "speech_to_text", "RealtimeSTT.AudioToTextRecorder"):
        logging.getLogger(stt_logger).setLevel(logging.WARNING)

    # Silence external STT/back-end libraries that are too chatty for normal runs.
    # Keep faster_whisper at WARNING (suppress INFO) and phonemizer at ERROR (suppress WARNING).
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    logging.getLogger("phonemizer").setLevel(logging.ERROR)

    return log_file


def pretty_json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value

    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)