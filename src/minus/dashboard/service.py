"""Talking to systemd about the assistant.

Restarting cannot go over the control socket: the socket can ask the process
to exit, and then there is nothing left to ask to start it again. That is a
service manager's job.

`systemctl --user` reaches the per-*user* manager through
$XDG_RUNTIME_DIR/systemd/private, which is scoped to the user rather than to a
login session -- so this works from a dashboard opened on any tty, not only
the one that started the service.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

UNIT = "minus.service"
TIMEOUT = 5.0


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def available() -> bool:
    return shutil.which("systemctl") is not None


def status() -> dict:
    """What systemd thinks of the unit.

    `LoadState=not-found` is not an error -- plenty of people will run
    `minus serve` in a tmux pane and never install a unit. The dashboard says
    "not managed by systemd" and disables restarting, rather than showing a
    failure for something nobody asked for.
    """
    if not available():
        return {"managed": False, "reason": "systemctl is not installed"}

    result = _run(
        ["show", UNIT, "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStartTimestamp"]
    )
    if result.returncode != 0:
        return {"managed": False, "reason": result.stderr.strip() or "systemctl failed"}

    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") != "loaded":
        return {"managed": False, "reason": "no minus.service unit is installed"}

    return {
        "managed": True,
        "active": values.get("ActiveState", "unknown"),
        "sub": values.get("SubState", ""),
        "pid": values.get("MainPID", ""),
        "since": values.get("ExecMainStartTimestamp", ""),
    }


def restart() -> tuple[bool, str]:
    """Restart the unit, returning success and something to display."""
    if not available():
        return False, "systemctl is not installed"

    result = _run(["restart", UNIT])
    if result.returncode == 0:
        return True, f"restarted {UNIT}"
    return False, (result.stderr.strip() or f"systemctl restart failed ({result.returncode})")
