"""Logging setup for MINUS.

Two things beyond plain `basicConfig`:

  * Several dependencies (RealtimeSTT, faster_whisper, phonemizer) log at
    volumes that bury our own output. They are quietened by logger name, and a
    filter catches anything that routes around that by logging through the
    root logger directly.
  * Each run writes its own log file. That directory previously grew without
    bound; it is now pruned to the most recent `retention` runs.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from minus.paths import logs_dir

# Chatty loggers and the level each is capped at.
_NOISY_LOGGERS: dict[str, int] = {
    "openai": logging.WARNING,
    "openai._base_client": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "RealtimeSTT": logging.WARNING,
    "RealtimeSTT.AudioToTextRecorder": logging.WARNING,
    "minus.audio.stt": logging.WARNING,
    "faster_whisper": logging.WARNING,
    "phonemizer": logging.ERROR,
}

_SUPPRESSED_ORIGINS = ("faster_whisper", "phonemizer", "RealtimeSTT", "minus.audio.stt")


class _ModuleSuppressFilter(logging.Filter):
    """Drop records originating from known-noisy modules.

    Matches on the record's origin only. Matching the formatted message would
    drop our own logs whenever a blocked name appeared in the payload -- for
    instance a workspace directory listing that happens to contain stt.py.
    """

    def __init__(self, blocked: tuple[str, ...]) -> None:
        super().__init__()
        self.blocked = tuple(b for b in blocked if b)

    def filter(self, record: logging.LogRecord) -> bool:
        name = getattr(record, "name", "") or ""
        module = getattr(record, "module", "") or ""
        pathname = getattr(record, "pathname", "") or ""
        return not any(b in name or b in module or b in pathname for b in self.blocked)


def prune_old_logs(directory: Path, retention: int, prefix: str = "run") -> int:
    """Delete all but the `retention` most recent logs, returning the count.

    Scoped to one prefix so that each kind of process keeps its own history:
    a dashboard opened twenty times in an afternoon must not evict the
    assistant's run logs, which are the ones worth reading.
    """
    if retention <= 0:
        return 0

    runs = sorted(directory.glob(f"{prefix}-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in runs[retention:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            # A log we cannot delete is not worth failing startup over.
            pass
    return removed


def setup_logging(
    level: int | str = logging.DEBUG,
    console_level: int | str = logging.INFO,
    retention: int = 30,
    *,
    console: bool = True,
    prefix: str = "run",
) -> Path:
    """Configure root logging and return the path of this run's log file.

    `console=False` omits the stderr handler entirely. Two callers need that
    and for opposite reasons: a service has no terminal to write to and would
    only duplicate the file log into the journal, and a full-screen TUI has a
    terminal it is drawing on, which a stray warning would paint over.

    `prefix` names the file. The dashboard uses its own so that it does not
    appear in its own log viewer, which follows the newest `run-*.log`.
    """
    directory = logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    prune_old_logs(directory, retention, prefix)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_file = directory / f"{prefix}-{timestamp}-{os.getpid()}.log"

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    suppress_filter = _ModuleSuppressFilter(_SUPPRESSED_ORIGINS)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    file_handler.addFilter(suppress_filter)

    root_logger.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(console_level)
        stream_handler.addFilter(suppress_filter)
        root_logger.addHandler(stream_handler)

    for logger_name, logger_level in _NOISY_LOGGERS.items():
        logging.getLogger(logger_name).setLevel(logger_level)

    return log_file
