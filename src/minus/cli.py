"""Command line entry point for MINUS.

Argument parsing and one function per subcommand. The object graph is built in
`assembly.py` and driven by `runtime.py`; this module decides which of those to
invoke and how to report the result to a terminal.

Heavy imports are deferred into the commands that need them. `minus memory` and
`minus calibrate` must work on a machine with no working PortAudio, `minus
serve` must never pay for textual, and importing either costs seconds even
where it succeeds.
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import signal
import time
from pathlib import Path
from queue import Queue

from minus.assembly import build_fast_tools, run_assistant
from minus.config import Settings, load_settings
from minus.core.escalation import DeepThinker
from minus.logging_config import redirect_console, setup_logging
from minus.paths import control_socket, project_root, semantic_memory_db

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minus", description="The MINUS voice assistant")
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Use terminal input instead of microphone speech recognition",
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="Do not listen on the control socket (lets a second MINUS run alongside one)",
    )

    subcommands = parser.add_subparsers(dest="command")

    say = subcommands.add_parser("say", help="Send a line to the running assistant")
    say.add_argument("text", nargs="+", help="What to say")

    status = subcommands.add_parser("status", help="Print what the running assistant is doing")
    status.add_argument("--watch", action="store_true", help="Keep printing as the state changes")

    serve = subcommands.add_parser("serve", help="Run headless, for a service manager")
    # SUPPRESS so that omitting it here leaves the root-level flag alone rather
    # than overwriting it with this parser's default.
    serve.add_argument(
        "--no-mic",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Take input only from the control socket",
    )

    subcommands.add_parser("systemd-unit", help="Print a systemd --user unit for this checkout")

    dash = subcommands.add_parser("dash", help="Open the management dashboard")
    dash.add_argument(
        "--unicode",
        action="store_true",
        help="Use box-drawing borders (for a terminal emulator rather than a VT)",
    )

    memory = subcommands.add_parser("memory", help="Interactively prune stored facts")
    memory.add_argument("--db", default=None, help="Path to the semantic memory database")
    memory.add_argument(
        "--include-inactive", action="store_true", help="Also show superseded facts"
    )

    subcommands.add_parser(
        "calibrate", help="Print similarity distributions and a suggested relevance threshold"
    )
    subcommands.add_parser("tools", help="List the tools available to the assistant")

    return parser


def _install_stack_dumper() -> None:
    """Allow a wedged process to be inspected without killing it.

    When the recorder/TTS pipeline wedges, Ctrl-C is not always reliable --
    whatever is stuck may be blocked in native code that never checks for
    interrupts. `kill -USR1 <pid>` dumps every thread's live Python stack to
    stderr, which pinpoints exactly what is blocked.
    """
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1)


def run_memory_tui(args) -> None:
    from minus.scripts.memory_tui import run_memory_tui as tui

    tui(args.db or str(semantic_memory_db()), include_inactive=args.include_inactive)


def run_tools() -> None:
    # Built through the same tiering the assistant uses, so `escalate` is
    # listed rather than being invisible until it is called.
    thinker = DeepThinker(model=None, deep_model="", results=Queue())
    fast_tools = build_fast_tools(thinker)
    thinker.shutdown()

    for schema in fast_tools.schemas():
        function = schema["function"]
        required = set(function["parameters"].get("required", []))
        params = ", ".join(
            name if name in required else f"{name}?"
            for name in function["parameters"]["properties"]
        )
        print(f"{function['name']}({params})\n    {function['description']}")


def _describe(snapshot: dict) -> str:
    """One line of status, for a terminal rather than a dashboard."""
    deep = snapshot.get("deep") or {}
    conversation = snapshot.get("conversation") or {}

    line = f"{snapshot.get('phase', '?'):<10}"
    if conversation.get("id"):
        line += f"  conversation {conversation['id']} ({conversation.get('message_count', 0)} msgs)"
    if deep.get("in_flight"):
        line += f"  [deep: {deep.get('elapsed_seconds', 0):.0f}s -- {deep.get('question')}]"
    return line


def run_say(args) -> int:
    """Hand a line to the running assistant, as though it had been spoken."""
    from minus.control.client import ControlClient, NotRunning

    try:
        with ControlClient(control_socket()) as client:
            client.request("say", text=" ".join(args.text))
    except NotRunning as exc:
        print(exc)
        return 1
    return 0


def run_status(args) -> int:
    """Print what the assistant is doing, once or until interrupted."""
    from minus.control.client import ControlClient, NotRunning

    try:
        if not args.watch:
            with ControlClient(control_socket()) as client:
                print(_describe(client.request("get_status")))
            return 0

        with ControlClient(
            control_socket(),
            on_event=lambda frame: print(_describe(frame.get("data") or {})),
        ) as client:
            client.subscribe()
            # Nothing to do but wait: the event handler does the printing, and
            # the reader thread does the reading.
            while True:
                time.sleep(0.5)
    except NotRunning as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        return 0


def run_serve(settings: Settings, use_mic: bool) -> None:
    """Run with no terminal, for a service manager to supervise.

    Differs from the interactive path only in what it does *not* do: no stderr
    handler, because journald already receives the file log's contents once and
    does not need them twice, and no CLI transcript source, because there is no
    stdin worth reading.

    Its own stdout and stderr are captured to a file first, before anything has
    had a chance to write to them. Without a terminal they otherwise go to
    /dev/null and the journal, so the dependencies' unlogged chatter -- and a
    faulthandler dump, which is exactly what you want when this hangs -- were
    somewhere the dashboard could not follow.
    """
    console_file = redirect_console(retention=settings.log_retention)
    log_file = setup_logging(
        level=settings.log_level,
        retention=settings.log_retention,
        console=False,
    )
    logger.info("Serving; logging to %s, console to %s", log_file, console_file)

    _install_stack_dumper()
    run_assistant(settings, use_mic=use_mic, control=True, interactive=False)


def run_dashboard(args) -> None:
    """Open the TUI.

    Imported here rather than at module scope, matching what this module
    already does for audio: `minus serve` must never pay for textual, and a
    machine without the extra installed must still be able to run everything
    else.
    """
    try:
        from minus.dashboard.app import run_dashboard as open_dashboard
    except ImportError as exc:
        raise SystemExit(
            f"The dashboard needs its extra: uv sync --extra dashboard  ({exc})"
        ) from exc

    # console=False, or a stray warning paints over the screen. The `dash`
    # prefix keeps this file out of the dashboard's own log viewer, which
    # follows the newest run-*.log, and out of the run logs' retention count.
    setup_logging(console=False, prefix="dash")
    open_dashboard(control_socket(), unicode_borders=args.unicode)


def run_systemd_unit() -> None:
    import sys

    from minus.control.systemd import render_unit

    # sys.executable is the interpreter; the console script beside it is what
    # has the entry point.
    console_script = Path(sys.executable).with_name("minus")
    print(render_unit(console_script, project_root()), end="")


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()

    if args.command == "systemd-unit":
        run_systemd_unit()
        return

    if args.command == "dash":
        run_dashboard(args)
        return

    if args.command == "say":
        raise SystemExit(run_say(args))

    if args.command == "status":
        raise SystemExit(run_status(args))

    if args.command == "memory":
        run_memory_tui(args)
        return

    if args.command == "calibrate":
        from minus.scripts.calibrate import run_calibration

        run_calibration()
        return

    if args.command == "tools":
        run_tools()
        return

    if args.command == "serve":
        run_serve(settings, use_mic=not args.no_mic)
        return

    log_file = setup_logging(
        level=settings.log_level,
        console_level=settings.console_log_level,
        retention=settings.log_retention,
    )
    logger.info("Logging to %s", log_file)

    _install_stack_dumper()
    run_assistant(settings, use_mic=not args.no_mic, control=not args.no_control)


if __name__ == "__main__":
    main()
