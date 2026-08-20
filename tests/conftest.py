from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def short_socket_path():
    """A socket path guaranteed to fit in sun_path.

    pytest's `tmp_path` embeds the test's own name, so a descriptive test in a
    descriptive file can push an AF_UNIX path past the 108-byte limit -- where
    it is silently truncated and the failure looks like a connection refused to
    a directory that does exist. A short mkdtemp under /tmp side-steps it.
    """
    directory = Path(tempfile.mkdtemp(prefix="minus-t", dir="/tmp"))
    try:
        yield directory / "c.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)
