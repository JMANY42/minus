"""Log file naming, retention scoping, and the console handler seam.

`setup_logging` reconfigures the root logger, so every test here restores it
afterwards -- otherwise the first one to run would redirect the rest of the
suite's logging into a temporary directory that no longer exists.
"""

from __future__ import annotations

import logging

import pytest

from minus import paths
from minus.logging_config import prune_old_logs, setup_logging


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """Point the whole path layer at a temp root, and undo the root logger."""
    monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield tmp_path / "logs"
    for handler in root.handlers[:]:
        handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def handler_kinds() -> list[str]:
    return [type(handler).__name__ for handler in logging.getLogger().handlers]


class TestConsoleHandler:
    def test_console_is_on_by_default(self, logs):
        setup_logging()

        assert handler_kinds() == ["FileHandler", "StreamHandler"]

    def test_console_false_leaves_only_the_file(self, logs):
        """A service has no terminal, and a TUI is drawing on the one it has."""
        setup_logging(console=False)

        assert handler_kinds() == ["FileHandler"]

    def test_the_file_is_written_either_way(self, logs):
        log_file = setup_logging(console=False)
        logging.getLogger("minus.test").warning("still recorded")

        assert log_file.exists()
        assert "still recorded" in log_file.read_text(encoding="utf-8")


class TestFileNaming:
    def test_runs_are_named_run_by_default(self, logs):
        assert setup_logging().name.startswith("run-")

    def test_prefix_names_the_file(self, logs):
        assert setup_logging(prefix="dash").name.startswith("dash-")


class TestRetention:
    def test_keeps_only_the_most_recent(self, tmp_path):
        for index in range(5):
            (tmp_path / f"run-{index}.log").write_text("x", encoding="utf-8")

        removed = prune_old_logs(tmp_path, retention=2)

        assert removed == 3
        assert len(list(tmp_path.glob("run-*.log"))) == 2

    def test_retention_is_scoped_to_one_prefix(self, tmp_path):
        """Opening the dashboard repeatedly must not evict the assistant's logs."""
        for index in range(4):
            (tmp_path / f"run-{index}.log").write_text("x", encoding="utf-8")
            (tmp_path / f"dash-{index}.log").write_text("x", encoding="utf-8")

        prune_old_logs(tmp_path, retention=1, prefix="dash")

        assert len(list(tmp_path.glob("dash-*.log"))) == 1
        assert len(list(tmp_path.glob("run-*.log"))) == 4

    def test_zero_retention_keeps_everything(self, tmp_path):
        (tmp_path / "run-1.log").write_text("x", encoding="utf-8")

        assert prune_old_logs(tmp_path, retention=0) == 0
        assert len(list(tmp_path.glob("run-*.log"))) == 1


class TestControlSocket:
    def test_prefers_the_runtime_directory(self, monkeypatch):
        monkeypatch.delenv("MINUS_CONTROL_SOCKET", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")

        assert paths.control_socket() == paths.Path("/run/user/4242/minus/control.sock")

    def test_falls_back_to_tmp_without_a_runtime_directory(self, monkeypatch):
        """A bare tty login without logind has no XDG_RUNTIME_DIR."""
        monkeypatch.delenv("MINUS_CONTROL_SOCKET", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        socket = paths.control_socket()

        assert socket.parent.name.startswith("minus-")
        assert socket.name == "control.sock"

    def test_the_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MINUS_CONTROL_SOCKET", str(tmp_path / "custom.sock"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")

        assert paths.control_socket() == tmp_path / "custom.sock"

    def test_stays_well_inside_the_sun_path_limit(self, monkeypatch):
        """AF_UNIX truncates silently past 108 bytes rather than erroring."""
        monkeypatch.delenv("MINUS_CONTROL_SOCKET", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

        assert len(str(paths.control_socket()).encode()) < 108

    def test_is_not_under_the_project_root(self, monkeypatch, tmp_path):
        """A socket must not live somewhere that gets synced or backed up."""
        monkeypatch.delenv("MINUS_CONTROL_SOCKET", raising=False)
        monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))

        assert tmp_path not in paths.control_socket().parents
