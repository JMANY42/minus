"""Turning observations into a verdict, deterministically.

The one rule this module exists to enforce: **a file is never called clean
without a detonation.** Static analysis can say "this looks like a packed,
unsigned Windows executable with a double extension" and that is worth a lot,
but "nothing ran and nothing was seen" is not the same statement as "nothing
happened when it ran".

Weights and thresholds are here rather than scattered through the rules so that
changing them is one edit, and the reasons are returned alongside the label so a
person can disagree with the decision line by line.
"""

from __future__ import annotations

from dataclasses import dataclass

from minus.triage.review import ModelReview
from minus.triage.sandbox import SandboxReport
from minus.triage.static import StaticReport

CLEAN = "clean"
SUSPICIOUS = "suspicious"
MALICIOUS = "malicious"
NEEDS_REVIEW = "needs_review"

# Process names that mean the same thing in every sandbox report: something
# scriptable was launched. Absence of these is not innocence; presence is a
# strong signal.
SUSPICIOUS_PROCESSES = (
    "powershell",
    "pwsh",
    "wscript",
    "cscript",
    "mshta",
    "rundll32",
    "regsvr32",
    "certutil",
    "bitsadmin",
)

# Registry writes that only make sense as persistence.
PERSISTENCE_MARKERS = (
    "\\currentversion\\run",
    "\\currentversion\\runonce",
    "\\startup",
    "schtasks",
)

# "Talked to a lot of distinct places" is a rough but useful beaconing signal.
NETWORK_FANOUT_THRESHOLD = 5


@dataclass(frozen=True)
class Verdict:
    label: str
    score: int
    detonated: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "score": self.score,
            "detonated": self.detonated,
            "reasons": list(self.reasons),
        }


def runtime_signals(report: SandboxReport) -> list[str]:
    """Everything in a sandbox report that is evidence of behaviour."""
    signals: list[str] = []

    for detection in report.defender_detections:
        signals.append(f"defender detection: {detection}")

    for process in report.processes:
        lowered = process.lower()
        if any(name in lowered for name in SUSPICIOUS_PROCESSES):
            signals.append(f"launched {process}")

    for write in report.registry_writes:
        lowered = write.lower()
        if any(marker in lowered for marker in PERSISTENCE_MARKERS):
            signals.append(f"registry persistence: {write}")

    if len(set(report.network_endpoints)) >= NETWORK_FANOUT_THRESHOLD:
        signals.append(f"network fan-out: {len(set(report.network_endpoints))} endpoints")

    return signals


def decide(
    static: StaticReport,
    sandbox: SandboxReport | None,
    *,
    require_detonation: bool = True,
    suspicious_score: int = 2,
    review: ModelReview | None = None,
) -> Verdict:
    """Weigh a static report, an optional detonation and an optional model read.

    The model is advisory but asymmetrically so: it can escalate a verdict and
    can never clear one. `malicious` deletes immediately, `high` is suspicious,
    and a failed or medium review blocks `clean` because the one thing that must
    not happen is a confident release on incomplete evidence.
    """
    reasons = [f"static:{flag}" for flag in static.flags]
    review_risk = ""
    if review is not None:
        if review.available:
            review_risk = review.risk
            evidence = list(review.reasons) or [review.summary or "no detail"]
            reasons.extend(f"model:{item}" for item in evidence[:8])
        else:
            review_risk = "unavailable"
            reasons.append(f"model review unavailable: {review.error}")

    detonated = sandbox is not None and sandbox.detonated
    if review_risk == "malicious":
        reasons.append("model judged the code malicious")
        return Verdict(MALICIOUS, static.score, detonated, tuple(reasons))
    if review_risk == "high":
        reasons.append("model judged the code high risk")
        return Verdict(SUSPICIOUS, static.score, detonated, tuple(reasons))

    if sandbox is None or not sandbox.detonated:
        reasons.append("no detonation: static evidence only")
        return Verdict(NEEDS_REVIEW, static.score, False, tuple(reasons))

    signals = runtime_signals(sandbox)
    if signals:
        return Verdict(
            MALICIOUS,
            static.score,
            True,
            tuple(reasons + signals),
        )

    if static.score >= suspicious_score:
        reasons.append(f"static score {static.score} >= {suspicious_score} with no runtime signal")
        return Verdict(SUSPICIOUS, static.score, True, tuple(reasons))

    if review_risk in ("medium", "unavailable"):
        reasons.append(f"model review is {review_risk}; clean needs a clear review")
        return Verdict(NEEDS_REVIEW, static.score, True, tuple(reasons))

    reasons.append("detonated with no runtime signals")
    return Verdict(CLEAN, static.score, True, tuple(reasons))
