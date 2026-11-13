"""Running a file where it cannot hurt the host, and reading what happened.

`SandboxRunner` is the seam. What exists today is `DryRunRunner`, which executes
nothing and says so — that is a real, honest result, and the scoring function
treats "not detonated" as `needs_review` rather than as clean.

The Windows runner is deliberately *not* stubbed here. Docker's Windows
containers need a Windows kernel (Hyper-V or process isolation on a Windows
host); on Linux the honest options are a Windows VM under QEMU/KVM controlled
over WinRM/SSH, or a separate Windows machine. A class that pretended to do that
without a guest would be worse than no class: it would produce confident
verdicts from nothing. Until that guest exists, `triage/airlock.py` provides the
no-execution alternative: static archive and permission checks plus a bubblewrap
isolation primitive for anything that must run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxReport:
    """What a detonation observed. Every list is empty when it saw nothing."""

    engine: str
    detonated: bool
    defender_detections: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()
    network_endpoints: tuple[str, ...] = ()
    file_writes: tuple[str, ...] = ()
    registry_writes: tuple[str, ...] = ()
    notes: str = ""


@runtime_checkable
class SandboxRunner(Protocol):
    """Somewhere a file can be opened, and something that can report on it."""

    name: str

    def available(self) -> bool:
        """Whether a run would mean anything right now."""

    def run(self, artifacts: list[Path], *, timeout: float) -> SandboxReport:
        """Detonate `artifacts` and report. Must not raise on malware."""


@dataclass
class DryRunRunner:
    """The default: no guest, no execution, and no pretence of either.

    It exists so the pipeline is callable — and its verdicts are as careful as
    the absence of a detonation deserves. A file routed through this runner is
    never called clean.
    """

    name: str = "dry_run"
    reason: str = "No sandbox is configured; nothing was executed."

    def available(self) -> bool:
        return True

    def run(self, artifacts: list[Path], *, timeout: float) -> SandboxReport:
        return SandboxReport(
            engine=self.name,
            detonated=False,
            notes=f"{self.reason} Artifacts staged: {len(artifacts)}.",
        )
