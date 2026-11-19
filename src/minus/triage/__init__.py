"""Triage: decide whether a downloaded file is safe before it leaves quarantine.

Three stages, each usable on its own: `pipeline.TriagePipeline` (fetch, scan,
promote), `static` (offline inspection), `scoring` (the verdict rules). The
sandbox is a seam (`sandbox.SandboxRunner`); the Windows runner lands with the
Windows guest, because a runner without a guest would produce confident verdicts
from nothing.
"""

from __future__ import annotations

from minus.triage.pipeline import TriageError, TriagePipeline
from minus.triage.sandbox import DryRunRunner, SandboxReport, SandboxRunner
from minus.triage.scoring import (
    CLEAN,
    MALICIOUS,
    NEEDS_REVIEW,
    SUSPICIOUS,
    Verdict,
    decide,
)
from minus.triage.static import StaticReport, analyse, inspect
from minus.triage.trace import (
    JsonlTracer,
    NullTracer,
    TraceEvent,
    Tracer,
    new_op_id,
    read_trace,
    trace_span,
)

__all__ = [
    "CLEAN",
    "MALICIOUS",
    "NEEDS_REVIEW",
    "SUSPICIOUS",
    "DryRunRunner",
    "JsonlTracer",
    "NullTracer",
    "SandboxReport",
    "SandboxRunner",
    "StaticReport",
    "TraceEvent",
    "Tracer",
    "TriageError",
    "TriagePipeline",
    "Verdict",
    "analyse",
    "decide",
    "inspect",
    "new_op_id",
    "read_trace",
    "trace_span",
]
