"""Approval for the coding mode, without blocking Textual's loop.

`CodingSession` asks an `Approver` on the worker thread that is running the
tool loop. Textual may only be touched from its own thread, so the request is
handed over with `call_from_thread`, a modal screen collects the answer, and the
worker parks on an Event until the screen is dismissed. The Event is what keeps
the tool loop sequential: a second request cannot be asked while the first is
still waiting for a person.

The default on timeout is deny. An approval prompt with nobody in front of it
must fail closed, and a timed-out worker that went ahead anyway would be the one
place the harness could act without a human having said yes.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

logger = logging.getLogger(__name__)

DENY = False
ALLOW = True
ALWAYS = "always"

APPROVAL_TIMEOUT_SECONDS = 120.0


class ApprovalScreen(ModalScreen[bool | str]):
    """One request, three answers: yes, no, or yes to this kind from now on."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "deny", "no", show=False),
        Binding("n", "deny", "no", show=False),
        Binding("y", "allow", "yes", show=False),
        Binding("a", "always", "always", show=False),
    ]

    def __init__(self, action: str, detail: str) -> None:
        super().__init__()
        self.action_text = action
        self.detail_text = detail

    def compose(self) -> ComposeResult:
        # The card clips its own detail: a tool may propose pasting a file, and
        # a modal taller than the terminal loses the buttons rather than the
        # prose. The worker's request keeps the full detail either way.
        detail = self.detail_text
        if len(detail) > 800:
            detail = detail[:800] + "…"
        with Vertical(id="approval"):
            yield Static("APPROVAL", classes="approval-kicker")
            yield Static(self.action_text, id="approval-action")
            yield Static(detail, id="approval-detail")
            with Horizontal(id="approval-buttons"):
                yield Button("Yes", id="approve", variant="success")
                yield Button("No", id="deny", variant="error")
                yield Button("Always", id="always", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        choice = event.button.id or ""
        if choice == "approve":
            self.dismiss(ALLOW)
        elif choice == "always":
            self.dismiss(ALWAYS)
        else:
            self.dismiss(DENY)

    def action_allow(self) -> None:
        self.dismiss(ALLOW)

    def action_deny(self) -> None:
        self.dismiss(DENY)

    def action_always(self) -> None:
        self.dismiss(ALWAYS)


class ThreadApprover:
    """`CodingSession`'s approver, backed by a modal screen on the app thread.

    `timeout` is a backstop for a screen nobody answers, not a user-facing
    setting: the harness has no reason to make someone choose how long to stare
    at a prompt.
    """

    def __init__(self, app: Any, *, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self.app = app
        self.timeout = timeout
        self.always = False

    def approve(self, request: Any) -> bool:
        if self.always:
            return True

        action = str(getattr(request, "action", "run a tool"))
        detail = str(getattr(request, "detail", ""))
        decision = {"allowed": False, "always": False}
        settled = threading.Event()

        def settle(result: Any) -> None:
            # Runs on the app thread, once the screen is dismissed. Setting the
            # event last means the worker never reads a half-written decision.
            if result == ALWAYS:
                decision["allowed"] = True
                decision["always"] = True
            else:
                decision["allowed"] = bool(result)
            settled.set()

        screen = ApprovalScreen(action, detail)
        try:
            self.app.call_from_thread(self.app.push_screen, screen, settle)
        except Exception:
            # No running app, or a screen that would not mount: fail closed
            # rather than guess what the operator would have said.
            logger.warning("Could not ask about %s; denying", action, exc_info=True)
            return False

        if not settled.wait(self.timeout):
            self._dismiss_if_present(screen)
            logger.warning("Approval for %s timed out; denying", action)
            return False

        if decision["always"]:
            self.always = True
        return decision["allowed"]

    def _dismiss_if_present(self, screen: ApprovalScreen) -> None:
        """Take a timed-out card down, but never pop somebody else's screen.

        Called from the blocked worker, so the screen check and the pop both
        have to run on the app thread. `call_from_thread` blocks until they
        have, which is what makes the check meaningful: another card cannot
        replace this one between the two statements.
        """

        def dismiss() -> None:
            if screen in self.app.screen_stack:
                self.app.pop_screen()

        try:
            self.app.call_from_thread(dismiss)
        except Exception:  # pragma: no cover - shutdown races
            logger.debug("Could not dismiss a timed-out approval card", exc_info=True)
