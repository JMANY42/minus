"""`minus code` tools over a real filesystem and real shell.

The approver used here is the production `AlwaysApprover`, not a stand-in: what
is tested is that paths stay inside the workspace and that write/patch/shell
return the shapes the model reads.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

from minus.coding.tools import AlwaysApprover, CodingTools
from minus.errors import ToolError
from minus.tools.registry import ToolRegistry


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    return root


def registry(tmp_path: Path) -> ToolRegistry:
    instance = ToolRegistry()
    CodingTools(instance, workspace(tmp_path), AlwaysApprover()).register()
    return instance


def test_write_then_read_round_trips_and_creates_parents(tmp_path):
    tools = registry(tmp_path)

    written = tools.dispatch("write_file", '{"path": "a/b/c.txt", "content": "hi"}')
    read = tools.dispatch("read_file", '{"path": "a/b/c.txt"}')

    assert '"written": 2' in written
    assert '"content": "hi"' in read
    assert (workspace(tmp_path) / "a/b/c.txt").read_text() == "hi"


def test_paths_cannot_escape_the_workspace(tmp_path):
    tools = registry(tmp_path)

    for payload in (
        '{"path": "../escape.txt", "content": "x"}',
        '{"path": "/etc/passwd", "content": "x"}',
    ):
        with pytest.raises(ToolError):
            tools.dispatch("write_file", payload)
    assert not (tmp_path / "escape.txt").exists()


def test_apply_patch_requires_exactly_one_match(tmp_path):
    root = workspace(tmp_path)
    (root / "f.txt").write_text("one two one")
    tools = registry(tmp_path)

    with pytest.raises(ToolError):
        tools.dispatch("apply_patch", '{"path": "f.txt", "old": "absent", "new": "x"}')
    with pytest.raises(ToolError) as caught:
        tools.dispatch("apply_patch", '{"path": "f.txt", "old": "one", "new": "x"}')
    assert "2 times" in str(caught.value)

    result = tools.dispatch(
        "apply_patch", '{"path": "f.txt", "old": "one two one", "new": "patched"}'
    )
    assert '"replaced": 1' in result
    assert (root / "f.txt").read_text() == "patched"


def test_run_shell_reports_exit_code_and_kills_a_timeout(tmp_path):
    tools = registry(tmp_path)

    ok = tools.dispatch("run_shell", '{"command": "printf hi"}')
    assert '"exit_code": 0' in ok
    assert '"stdout": "hi"' in ok

    started = time.monotonic()
    timed_out = tools.dispatch(
        "run_shell",
        '{"command": "python3 -c \\"import time; time.sleep(30)\\"", "timeout_seconds": 1}',
    )
    elapsed = time.monotonic() - started

    assert '"timed_out": true' in timed_out
    assert elapsed < 10, elapsed


def test_non_interactive_stdin_requires_yes(tmp_path, monkeypatch):
    from minus import cli

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    args = types.SimpleNamespace(
        path=str(workspace(tmp_path)), yes=False, model=None, max_rounds=None
    )

    with pytest.raises(SystemExit) as caught:
        cli.run_code(args)

    assert "--yes is required" in str(caught.value)
