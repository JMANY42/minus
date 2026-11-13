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
import shutil
import signal
import sys
import time
from pathlib import Path
from queue import Queue

from minus.assembly import (
    build_alarm_tools,
    build_deep_tools,
    build_fast_tools,
    build_google_tools,
    build_triage_tools,
    build_vpn_tools,
    run_assistant,
)
from minus.config import Settings, load_settings
from minus.core.escalation import DeepThinker
from minus.logging_config import redirect_console, setup_logging
from minus.paths import control_socket, project_root, semantic_memory_db
from minus.tools.policy import build_policy

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
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Print replies instead of speaking them (no PortAudio, Kokoro or sound card)",
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
    serve.add_argument(
        "--no-audio",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print replies instead of speaking them",
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

    vm_image = subcommands.add_parser(
        "vm-image", help="Build the Windows sandbox base image (needs qemu and ISOs)"
    )
    vm_image.add_argument("--iso", required=True, help="Windows x64 installation ISO")
    vm_image.add_argument("--virtio-iso", required=True, help="virtio-win driver ISO")
    vm_image.add_argument(
        "--work-dir", required=True, help="Where base.qcow2 goes (needs >= 40 GiB free)"
    )
    vm_image.add_argument("--sysmon-zip", default=None, help="Optional Sysmon zip to install")

    subcommands.add_parser("vm-check", help="Report whether the Windows sandbox could run")
    subcommands.add_parser("vpn-check", help="Report whether the VPN preflight passes")

    vpn = subcommands.add_parser("vpn", help="Show, connect or disconnect the AirVPN tunnel")
    vpn.add_argument(
        "action",
        nargs="?",
        choices=("status", "connect", "disconnect"),
        default="status",
        help="What to do (default: status)",
    )
    vpn.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    qbt = subcommands.add_parser("qbt", help="Provision or inspect qBittorrent")
    qbt.add_argument(
        "action",
        nargs="?",
        choices=("setup", "status"),
        default="status",
        help="Provision (start + preferences) or print status",
    )

    triage = subcommands.add_parser("triage", help="Ingest, scan, verdict or promote a torrent")
    triage.add_argument(
        "action",
        nargs="?",
        choices=("ingest", "scan", "verdict", "promote"),
        default="ingest",
        help="Stage of the pipeline (default: ingest)",
    )
    triage.add_argument("target", nargs="?", help="Magnet/http URL to ingest, or info hash")
    triage.add_argument("--name", default="", help="Library name for promote")
    triage.add_argument("--no-detonate", action="store_true", help="Skip the sandbox stage")

    harness = subcommands.add_parser("harness", help="Open the interactive MINUS harness")
    harness.add_argument(
        "--workspace", default=".", help="Coding workspace (default: current directory)"
    )
    harness.add_argument("--model", default=None, help="Conversational model for this session")

    subcommands.add_parser("voice", help="Run the original voice assistant (microphone)")
    subcommands.add_parser(
        "mcp", help="Serve the MINUS tools over the Model Context Protocol (stdio)"
    )

    doctor = subcommands.add_parser(
        "doctor", help="Report the security posture without changing anything"
    )
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    secrets_scan = subcommands.add_parser(
        "secrets-scan",
        help="Find key-shaped strings in a tree (prints the file and kind, never the value)",
    )
    secrets_scan.add_argument("--path", default=".", help="Directory to scan (default: cwd)")

    code = subcommands.add_parser("code", help="Open a workspace coding session")
    code.add_argument("path", nargs="?", default=".", help="Workspace directory (default: cwd)")
    code.add_argument(
        "--yes",
        action="store_true",
        help="Approve every write and shell command without asking (required on a non-tty)",
    )
    code.add_argument("--model", default=None, help="Model for this session")
    code.add_argument("--max-rounds", type=int, default=None, help="Tool rounds per turn")

    model = subcommands.add_parser("model", help="Show or choose the model each tier uses")
    model.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Conversational model id, e.g. openai/gpt-oss-20b (omit to print the current choice)",
    )
    model.add_argument("--deep", default=None, metavar="MODEL", help="Model for the deep tier")
    model.add_argument(
        "--extraction",
        default=None,
        metavar="MODEL",
        help="Model used to extract facts after a conversation",
    )
    model.add_argument(
        "--list",
        nargs="?",
        const="",
        default=None,
        metavar="FILTER",
        help="List model ids OpenRouter offers, optionally filtered",
    )

    google = subcommands.add_parser(
        "google-auth", help="Connect a Google account so the task tools can reach it"
    )
    google.add_argument("--client-id", default="", help="OAuth client ID (else .env, else asked)")
    google.add_argument(
        "--client-secret", default="", help="OAuth client secret (else .env, else asked)"
    )
    google.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=None,
        help="Write the credentials to .env without asking",
    )
    google.add_argument(
        "--no-write",
        dest="write",
        action="store_false",
        help="Only print the credentials, leaving .env alone",
    )

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
    # listed rather than being invisible until it is called -- and grouped by
    # tier, because the two do not hold the same set and a flat list could not
    # say which one a tool belonged to.
    thinker = DeepThinker(model=None, deep_model="", results=Queue())
    settings = load_settings()
    from minus.plugins import LuaPluginHost, lua_available

    plugins = LuaPluginHost() if settings.plugins_enabled and lua_available() else None
    policy = build_policy(
        conversational=build_fast_tools(
            thinker,
            [
                *build_google_tools(settings),
                build_alarm_tools(settings),
                *build_triage_tools(settings),
                *build_vpn_tools(settings),
            ],
            plugins=plugins,
        ),
        deep=build_deep_tools(),
    )
    thinker.shutdown()
    # The stored switches too: a tool listed as available while .env has it
    # switched off would be the listing lying about the assistant.
    policy.apply_spec(settings.disabled_tools)

    for agent in policy.agents:
        print(f"{agent.title}:")
        if agent.registry is None:
            print(f"    ({agent.note})")
            continue
        for name in agent.registry.names():
            schema = agent.registry.get(name).schema["function"]
            required = set(schema["parameters"].get("required", []))
            params = ", ".join(
                argument if argument in required else f"{argument}?"
                for argument in schema["parameters"]["properties"]
            )
            state = "" if agent.registry.enabled(name) else "  [off]"
            print(f"    {name}({params}){state}\n        {schema['description']}")


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


def run_model(args) -> int:
    """Print the tier models, choose new ones, or list candidates."""
    from minus.scripts.models import apply, configured, list_candidates, resolve

    settings = load_settings()

    if args.list is not None:
        try:
            candidates = list_candidates(settings, args.list)
        except Exception as exc:
            print(f"Could not read the model catalogue: {exc}")
            return 1
        if not candidates:
            print(f"No model matches {args.list!r}.")
            return 1
        width = max(len(str(model.get("id", ""))) for model in candidates)
        for candidate in candidates:
            line = f"{candidate.get('id', ''):<{width}}"
            if candidate.get("context_length"):
                line += f"  {candidate['context_length']} ctx"
            name = candidate.get("name")
            if name and name != candidate.get("id"):
                line += f"  {name}"
            print(line.rstrip())
        return 0

    if args.name is None and args.deep is None and args.extraction is None:
        for tier, name in configured(settings).items():
            print(f"{tier:<15}{name}")
        return 0

    try:
        changes = resolve(
            settings, conversational=args.name, deep=args.deep, extraction=args.extraction
        )
    except ValueError as exc:
        print(exc)
        return 1

    result = apply(changes, settings)

    labels = {
        "chat_model": "conversational",
        "deep_model": "deep",
        "fact_extraction_model": "extraction",
    }
    for name in result.get("applied", []):
        print(f"applied   {labels.get(name, name)} = {changes.get(name, '')}")
    for key in result.get("persisted", []):
        setting = key.removeprefix("MINUS_").lower()
        print(f"persisted {labels.get(setting, key)} = {changes.get(setting, '')}")
    for name in result.get("restart_required", []):
        value = changes.get(name, "")
        print(f"pending   {labels.get(name, name)} = {value} (takes effect on next start)")
    for name, reason in (result.get("rejected") or {}).items():
        print(f"rejected  {labels.get(name, name)}: {reason}")

    if not result.get("live"):
        print("MINUS is not running; the change was written to .env for the next start.")
    return 0


def run_vpn_check() -> int:
    """Print the tunnel state the triage preflight would see."""
    from minus.services.vpn import tunnel_status

    settings = load_settings()
    snapshot = tunnel_status(settings)
    print(_vpn_report(settings, snapshot))
    return 0


def _vpn_report(settings: Settings, snapshot) -> str:
    return "\n".join(
        [
            f"provider:  {settings.vpn_provider or '(unset)'}",
            f"interface: {snapshot.interface or '(unset)'}",
            f"up:        {'yes' if snapshot.link_up else 'no'}",
            f"carrying:  {'yes' if snapshot.carrying_traffic else 'no'}",
            f"detail:    {snapshot.detail}",
            f"triage gate: {'enforced' if settings.require_vpn_for_triage else 'off'}",
        ]
    )


def run_vpn_command(args) -> int:
    """Status, connect or disconnect the AirVPN tunnel."""
    import json

    from minus.errors import MinusError
    from minus.services.vpn import VpnError, connect, disconnect, tunnel_status

    settings = load_settings()
    try:
        if args.action == "connect":
            snapshot = connect(settings)
        elif args.action == "disconnect":
            result = disconnect(settings)
            print(json.dumps(result) if args.json else result.get("output", ""))
            return 0
        else:
            snapshot = tunnel_status(settings)
    except (VpnError, MinusError) as exc:
        print(f"vpn: {exc}")
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "interface": snapshot.interface,
                    "link_up": snapshot.link_up,
                    "carrying_traffic": snapshot.carrying_traffic,
                    "healthy": snapshot.healthy,
                    "detail": snapshot.detail,
                }
            )
        )
    else:
        print(_vpn_report(settings, snapshot))
    return 0 if snapshot.healthy or args.action == "status" else 1


def run_qbt_command(args) -> int:
    """Provision qBittorrent or print its state."""

    from minus.errors import MinusError

    settings = load_settings()
    try:
        from minus.scripts.qbt import ensure_running, provision

        if args.action == "setup":
            report = provision(settings)
            print(f"url:       {report.get('url')}")
            print(f"username:  {report.get('username')}")
            print(f"interface: {report.get('interface') or '(not bound)'}")
            print(f"incoming:  {report.get('incoming')}")
            print(f"started:   {'yes' if report.get('started') else 'no'}")
            return 0

        client = ensure_running(settings)
        print(f"version:   {client.version()}")
        info = client.transfer_info()
        print(f"download:  {info.get('dl_info_speed', 0)} B/s")
        print(f"upload:    {info.get('up_info_speed', 0)} B/s")
        torrents = client.torrents()
        print(f"torrents:  {len(torrents)}")
        for torrent in torrents[:10]:
            print(
                f"  {torrent.get('hash', '')[:12]}  "
                f"{float(torrent.get('progress') or 0):>5.0%}  {torrent.get('name', '')}"
            )
        return 0
    except (MinusError, OSError) as exc:
        print(f"qbt: {exc}")
        return 1


def run_triage_command(args) -> int:
    """Drive one stage of the torrent triage pipeline."""
    import json

    from minus.assembly import build_triage_pipeline

    settings = load_settings()
    pipeline = build_triage_pipeline(settings)
    if pipeline is None:
        print("qBittorrent is not configured (set MINUS_QBITTORRENT_URL).")
        return 1
    if not args.target:
        print("triage needs a magnet/http URL (ingest) or an info hash.")
        return 1

    try:
        if args.action == "ingest":
            result = pipeline.ingest(args.target, detonate=not args.no_detonate)
        elif args.action == "scan":
            result = pipeline.scan(args.target, detonate=not args.no_detonate)
        elif args.action == "verdict":
            result = pipeline.verdict(args.target)
        else:
            result = pipeline.promote(args.target, name=args.name)
    except Exception as exc:  # pipeline errors are MinusError subclasses
        print(f"triage: {exc}")
        return 1

    print(json.dumps(result, indent=2, default=str))
    if isinstance(result, dict) and result.get("alert"):
        print(f"\nALERT: {result['alert']}")
    if isinstance(result, dict) and result.get("status") == "failed":
        return 1
    return 0


def run_harness_command(args) -> int:
    """Open the interactive harness."""
    from pathlib import Path

    try:
        from minus.harness import run_harness
    except ImportError as exc:
        print(
            f"The harness needs Textual ({exc}). Install it with "
            "`uv sync --extra harness`, or run `minus voice` for the terminal loop."
        )
        return 1

    settings = load_settings()
    run_harness(settings, workspace=Path(args.workspace).expanduser(), model=args.model)
    return 0


def run_doctor(args) -> int:
    """Print the security posture; reads only, and never a secret value."""
    from minus.doctor import collect_posture, format_posture, posture_json

    settings = load_settings()
    posture = collect_posture(settings)
    print(posture_json(posture) if args.json else format_posture(posture))
    return 0


def run_secrets_scan(args) -> int:
    """Report key-shaped strings, naming the file and kind but never the value.

    Exit code 1 on findings so this is usable in a pre-commit hook or CI step
    without parsing stdout.
    """
    from minus.security import scan_file, scan_repository

    root = Path(args.path).expanduser()
    if not root.exists():
        print(f"Not found: {root}")
        return 1

    # A single file is a legitimate target (checking one diff), and rglob on a
    # file would silently find nothing.
    findings = scan_file(root) if root.is_file() else scan_repository(root)
    if not findings:
        print("no findings")
        return 0

    for finding in findings:
        print(f"{finding['path']}:{finding['line']} {finding['kind']}")
    return 1


def run_code(args) -> int:
    """Open a coding session in a workspace."""
    from minus.coding.session import CodingSession
    from minus.coding.tools import AlwaysApprover, ConsoleApprover
    from minus.scripts.models import resolve

    settings = load_settings()
    workspace = Path(args.path).expanduser().resolve()
    if not workspace.is_dir():
        print(f"Not a directory: {workspace}")
        return 1
    if not (settings.llm_api_key or settings.openrouter_api_key):
        print(
            "Set a key first: OPENROUTER_API_KEY, or MINUS_LLM_API_KEY for a direct "
            "provider such as DeepSeek (with MINUS_LLM_BASE_URL)."
        )
        return 1

    if args.model:
        try:
            settings.chat_model = resolve(settings, conversational=args.model)["chat_model"]
        except ValueError as exc:
            print(exc)
            return 1

    if not sys.stdin.isatty() and not args.yes:
        raise SystemExit(
            "--yes is required when stdin is not a terminal: the approval gate would have "
            "no one to ask."
        )

    approver = AlwaysApprover() if args.yes else ConsoleApprover()
    CodingSession(
        settings,
        workspace,
        approver=approver,
        max_tool_rounds=args.max_rounds,
    ).run()
    return 0


def run_vm_image(args) -> int:
    """Build the Windows sandbox base image, once."""
    from minus.errors import MinusError
    from minus.triage.vm_build import build_base_image

    settings = load_settings()
    if not settings.windows_winrm_password:
        print("Set MINUS_WINDOWS_WINRM_PASSWORD before building an image.")
        return 1

    try:
        base = build_base_image(
            settings,
            iso=Path(args.iso).expanduser(),
            virtio_iso=Path(args.virtio_iso).expanduser(),
            work_dir=Path(args.work_dir).expanduser(),
            sysmon_zip=Path(args.sysmon_zip).expanduser() if args.sysmon_zip else None,
        )
    except MinusError as exc:
        print(exc)
        return 1

    print(f"base image: {base}")
    print(f"set MINUS_WINDOWS_BASE_IMAGE={base} and MINUS_WINDOWS_WORK_DIR={base.parent}")
    return 0


def run_vm_check() -> int:
    """Say whether the sandbox could run, and what is missing if not."""
    from minus.triage.windows_vm import runner_from_settings

    settings = load_settings()
    runner = runner_from_settings(settings)
    reason = runner.unavailable_reason()

    print(f"available: {'yes' if not reason else 'no'}")
    if reason:
        print(f"reason:    {reason}")
    if runner.base_image.is_file():
        print(f"base image: {runner.base_image}")
        metadata = runner.base_image.with_name("base.json")
        if metadata.is_file():
            summary = metadata.read_text(encoding="utf-8").strip().replace("\n", " ")
            print(f"metadata:  {summary}")
    if settings.windows_work_dir and runner.work_dir.is_dir():
        free = shutil.disk_usage(runner.work_dir).free
        print(f"free space: {free / 1024**3:.1f} GiB in {runner.work_dir}")
    return 0


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


def run_serve(settings: Settings, use_mic: bool, text_only: bool = False) -> None:
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
    run_assistant(settings, use_mic=use_mic, control=True, interactive=False, text_only=text_only)


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

    if args.command == "model":
        raise SystemExit(run_model(args))

    if args.command == "vm-image":
        raise SystemExit(run_vm_image(args))

    if args.command == "code":
        raise SystemExit(run_code(args))

    if args.command == "vpn-check":
        raise SystemExit(run_vpn_check())

    if args.command == "mcp":
        from minus.mcp.server import main as mcp_main

        raise SystemExit(mcp_main())

    if args.command == "vpn":
        raise SystemExit(run_vpn_command(args))

    if args.command == "qbt":
        raise SystemExit(run_qbt_command(args))

    if args.command == "triage":
        raise SystemExit(run_triage_command(args))

    if args.command in (None, "harness"):
        # Bare `minus` is the harness now: one surface for chat, code, torrents
        # and the VPN. The voice loop keeps its own subcommand rather than
        # competing for the default.
        if args.command is None:
            args.workspace = "."
            args.model = None
        log_file = setup_logging(
            level=settings.log_level,
            console_level=settings.console_log_level,
            retention=settings.log_retention,
        )
        logger.info("Logging to %s", log_file)
        _install_stack_dumper()
        raise SystemExit(run_harness_command(args))

    if args.command == "vm-check":
        raise SystemExit(run_vm_check())

    if args.command == "doctor":
        raise SystemExit(run_doctor(args))

    if args.command == "secrets-scan":
        raise SystemExit(run_secrets_scan(args))

    if args.command == "google-auth":
        from minus.scripts.google_auth import run_google_auth

        raise SystemExit(run_google_auth(args.client_id, args.client_secret, write=args.write))

    if args.command == "serve":
        run_serve(settings, use_mic=not args.no_mic, text_only=args.no_audio)
        return

    if args.command == "voice":
        log_file = setup_logging(
            level=settings.log_level,
            console_level=settings.console_log_level,
            retention=settings.log_retention,
        )
        logger.info("Logging to %s", log_file)
        _install_stack_dumper()
        run_assistant(
            settings,
            use_mic=not args.no_mic,
            control=not args.no_control,
            text_only=args.no_audio,
        )
        return

    log_file = setup_logging(
        level=settings.log_level,
        console_level=settings.console_log_level,
        retention=settings.log_retention,
    )
    logger.info("Logging to %s", log_file)

    _install_stack_dumper()
    run_assistant(
        settings,
        use_mic=not args.no_mic,
        control=not args.no_control,
        text_only=args.no_audio,
    )


if __name__ == "__main__":
    main()
