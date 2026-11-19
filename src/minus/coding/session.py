"""`minus code`: a workspace REPL on top of the shared tool loop.

Nothing here is a second agent framework. A turn is `ToolLoop` with a coding
registry and a coding prompt; the transcript is an ordinary `Transcript` kept in
memory and written to `memory/coding_sessions/` after each turn. The only two
things this file owns are the slash commands and the fact that `/model` changes
the live `Settings` object — which works because `OpenRouterClient.complete`
reads `chat_model` per call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from minus import paths
from minus.coding.tools import Approver, CodingTools
from minus.config import Settings
from minus.core.loop import ToolLoop
from minus.core.messages import Message, Transcript
from minus.llm.client import OpenRouterClient
from minus.prompts import build_coding_prompt
from minus.scripts import models
from minus.services.json import write_json
from minus.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

HELP = """commands:
  /help            this list
  /model           show the model this session uses
  /model <name>    switch model for the rest of the session (not persisted)
  /clear           forget the transcript, keep the workspace
  /exit            leave (the transcript is saved)"""


class CodingSession:
    """One coding session: workspace, tools, transcript, model."""

    def __init__(
        self,
        settings: Settings,
        workspace: Path,
        *,
        approver: Approver,
        model: Any | None = None,
        max_tool_rounds: int | None = None,
        session_id: str | None = None,
        echo: Callable[[str], None] = print,
        observer: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = Path(workspace).resolve()
        self.approver = approver
        self.model = model if model is not None else OpenRouterClient(settings)
        self.max_tool_rounds = (
            settings.max_tool_rounds if max_tool_rounds is None else max_tool_rounds
        )
        self.session_id = session_id or (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        )
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.echo = echo
        self.observer = observer
        self.exiting = False

        self.transcript = Transcript()
        self.registry = ToolRegistry()
        CodingTools(self.registry, self.workspace, approver).register()

    # ---- One turn ----

    def run_turn(self, text: str) -> str:
        """Send one user message through the tool loop and print the answer."""
        # The user turn has to be in the transcript before the loop runs: the
        # loop completes against `transcript.to_wire()`, and without this the
        # model answers a conversation it never saw (found by a live run).
        self.transcript.append(Message.user(text))
        loop = ToolLoop(
            model=self.model,
            transcript=self.transcript,
            tools=self.registry,
            system_prompt=build_coding_prompt(self.workspace),
            max_tool_rounds=self.max_tool_rounds,
            observer=self.observer,
        )
        answer = loop.run().content or ""
        self.echo(answer)
        self.save()
        return answer

    # ---- Slash commands ----

    def command(self, line: str) -> bool:
        """Handle a slash command. Returns True when the line was one."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            return False

        name, _, argument = stripped.partition(" ")
        argument = argument.strip()

        if name == "/help":
            self.echo(HELP)
            return True

        if name == "/model":
            if not argument:
                self.echo(f"model: {self.settings.chat_model}")
                return True
            previous = self.settings.chat_model
            try:
                changes = models.resolve(self.settings, conversational=argument)
            except ValueError as exc:
                self.echo(f"model: {exc}")
                return True
            self.settings.chat_model = changes["chat_model"]
            self.echo(f"model: {previous} -> {self.settings.chat_model} (this session only)")
            return True

        if name == "/clear":
            self.transcript = Transcript()
            self.echo("cleared")
            return True

        if name in ("/exit", "/quit"):
            self.exiting = True
            return True

        self.echo(f"unknown command {name}; /help for the list")
        return True

    # ---- Persistence ----

    def save(self) -> Path:
        path = paths.coding_sessions_dir() / f"{self.session_id}.json"
        write_json(
            path,
            {
                "session_id": self.session_id,
                "workspace": str(self.workspace),
                "model": self.settings.chat_model,
                "started_at": self.started_at,
                "messages": self.transcript.to_wire(),
            },
        )
        return path

    # ---- REPL ----

    def run(self) -> None:
        self.echo(
            f"workspace: {self.workspace}\n"
            f"model:     {self.settings.chat_model}\n"
            f"tools:     {', '.join(self.registry.names())}\n"
            "/help for commands, /exit to leave"
        )
        while not self.exiting:
            try:
                line = input("minus> ")
            except (EOFError, KeyboardInterrupt):
                self.echo("")
                break

            line = line.strip()
            if not line:
                continue
            if self.command(line):
                continue

            try:
                self.run_turn(line)
            except Exception as exc:
                # A provider failure must not end the session: the transcript
                # on disk is the work, and the next turn can retry.
                logger.exception("coding turn failed")
                self.echo(f"turn failed: {exc}")

        self.save()
