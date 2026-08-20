"""The systemd unit, generated rather than kept as a file.

A tracked unit file drifts: it hard-codes a home directory and an interpreter
path, neither of which is knowable when it is written, and `hatchling` packages
only `src/minus` so it would not ship anyway. Rendering it from
`sys.executable` and `paths.project_root()` means the paths are right by
construction, and a pure function is something a test can hold to account.

A *user* unit, not a system one. The assistant's audio comes from the login
session's PipeWire, and a system service has no route to it.
"""

from __future__ import annotations

from pathlib import Path

UNIT_NAME = "minus.service"

# WorkingDirectory and MINUS_PROJECT_ROOT are both set, and neither is
# redundant: the first is what makes a relative `.env` resolve, the second is
# what makes paths.py agree with it no matter where the interpreter came from.
#
# TimeoutStopSec is generous because a graceful stop is not instant -- SIGTERM
# ends the conversation, which condenses the transcript and asks the model to
# extract facts from it. Cutting that short is exactly the data loss the
# handler exists to prevent.
_TEMPLATE = """\
[Unit]
Description=MINUS voice assistant
Documentation=https://github.com/JMANY42/minus
After=default.target
Wants=pipewire.service pipewire-pulse.service

[Service]
Type=simple
WorkingDirectory={root}
Environment=MINUS_PROJECT_ROOT={root}
ExecStart={python} serve
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=45
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
"""


def render_unit(python: Path | str, root: Path | str) -> str:
    """The unit file text for this checkout and this interpreter.

    `python` is the `minus` console script, not the interpreter itself --
    `sys.executable` points at python, and its sibling `minus` is what has the
    entry point wired up.
    """
    return _TEMPLATE.format(python=Path(python), root=Path(root))


def install_hint(unit_path: Path | str) -> str:
    """What to run after writing the unit.

    `enable-linger` is not optional. Without it a user manager is torn down at
    logout and the service goes with it, which is the exact opposite of what
    an always-on assistant is for.
    """
    return (
        f"Wrote {unit_path}\n"
        "\n"
        "  systemctl --user daemon-reload\n"
        "  systemctl --user enable --now minus\n"
        '  loginctl enable-linger "$USER"   # keeps it running after logout\n'
    )
