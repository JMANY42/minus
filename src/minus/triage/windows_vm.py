"""A Windows guest under QEMU/KVM, used as the detonation sandbox.

Why a VM and not a container: Docker's Windows containers need a Windows kernel
and therefore a Windows host. This machine is Linux, but has `/dev/kvm`, so a
fully virtualised guest runs at native speed. One immutable `base.qcow2` plus a
per-run overlay gives snapshot/rollback for free, and needs no Docker daemon and
no libvirt.

The network shape is the security property. `-netdev user,restrict=on` is
documented as: the guest cannot contact the host, and no guest packet is routed
outside; explicitly set forwarding rules are unaffected. So the only channel is
host→guest WinRM on `127.0.0.1:<port>`. Artifacts are pushed in base64 chunks
over that channel and the JSON report is pulled back the same way — there is no
guest→host path at all, so a sample inside the guest cannot reach the host's
localhost services or the internet.

Everything here is unit-tested with a fake WinRM session and a fake process; a
live run needs qemu, a Windows ISO, and a work directory with room for a 64 GiB
sparse image, none of which this machine has.
"""

from __future__ import annotations

import base64
import importlib.resources
import importlib.util
import json
import logging
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minus.errors import MinusError
from minus.triage.sandbox import SandboxReport

logger = logging.getLogger(__name__)

# 2 KiB decoded per WinRM call. The base64 text is embedded in a PowerShell
# command line, and Windows caps that at ~8 KiB; 8 KiB decoded produced ~11 KiB
# of base64 and failed with "The command line is too long."
CHUNK_BYTES = 2048

GUEST_ROOT = "C:\\triage"

# A torrent's filenames are attacker-controlled. On Windows a backslash is a
# separator, so `..\..\Windows\...` in a Linux filename would otherwise write
# outside the input directory inside the guest. Only a conservative alphabet
# survives, and the numeric prefix keeps reserved device names impossible.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
WINRM_URL = "http://127.0.0.1:{port}/wsman"


class WindowsVmError(MinusError):
    """The guest could not be started, staged, run or read."""


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _which(binary: str) -> Path | None:
    found = shutil.which(binary)
    return Path(found) if found else None


@dataclass
class WindowsVmRunner:
    """A SandboxRunner that detonates inside a Windows VM."""

    qemu: Path = Path("qemu-system-x86_64")
    qemu_img: Path = Path("qemu-img")
    base_image: Path = Path("")
    work_dir: Path = Path("")
    username: str = "minus"
    password: str = ""
    cpus: int = 4
    memory_mb: int = 6144
    boot_timeout: float = 420.0
    guest_timeout: float = 180.0
    max_artifact_bytes: int = 64 * 1024**2
    min_free_bytes: int = 20 * 1024**3
    name: str = "windows-vm"
    # Injectable so the whole flow is testable without qemu or Windows:
    # `winrm_factory(port)` returns an object with `run_ps(script)`.
    winrm_factory: Callable[[int], Any] | None = field(default=None, repr=False)
    popen: Callable[..., Any] = field(default=subprocess.Popen, repr=False)

    # ---- Availability ----

    def _resolved(self, candidate: Path) -> Path | None:
        if candidate.is_file():
            return candidate
        return _which(str(candidate))

    def unavailable_reason(self) -> str:
        """Why a run would mean nothing right now; `""` when it would."""
        if self._resolved(self.qemu) is None:
            return f"{self.qemu} not found on PATH (install qemu-full)"
        if self._resolved(self.qemu_img) is None:
            return f"{self.qemu_img} not found on PATH (part of qemu-full)"
        if not self.base_image.is_file():
            return f"base image {self.base_image} does not exist (run `minus vm-image`)"
        if not self.work_dir.is_dir():
            return f"work directory {self.work_dir} does not exist"
        try:
            free = shutil.disk_usage(self.work_dir).free
        except OSError as exc:
            return f"cannot read free space in {self.work_dir}: {exc}"
        if free < self.min_free_bytes:
            return (
                f"only {free / 1024**3:.1f} GiB free in {self.work_dir}; "
                f"at least {self.min_free_bytes / 1024**3:.0f} GiB required"
            )
        if importlib.util.find_spec("winrm") is None:
            return "pywinrm is not installed (uv sync --extra windows)"
        return ""

    def available(self) -> bool:
        return not self.unavailable_reason()

    # ---- Command ----

    def build_command(self, overlay: Path, port: int, run_dir: Path) -> list[str]:
        """The exact qemu argv. Kept separate so a test can assert it.

        SeaBIOS boots the IDE-installed base image, and the e1000 NIC is the
        one Windows installed its driver for; neither needs OVMF or virtio.
        """
        qemu = self._resolved(self.qemu) or self.qemu
        return [
            str(qemu),
            "-enable-kvm",
            "-machine",
            "pc",
            "-cpu",
            "host",
            "-smp",
            str(self.cpus),
            "-m",
            str(self.memory_mb),
            "-drive",
            f"file={overlay},if=ide,format=qcow2",
            "-netdev",
            f"user,id=n0,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:5985",
            "-device",
            "e1000,netdev=n0",
            "-display",
            "none",
            "-no-reboot",
            "-pidfile",
            str(run_dir / "qemu.pid"),
            "-chardev",
            f"file,id=ser0,path={run_dir / 'console.log'}",
            "-serial",
            "chardev:ser0",
        ]

    # ---- Run ----

    def run(self, artifacts: list[Path], *, timeout: float) -> SandboxReport:
        reason = self.unavailable_reason()
        if reason:
            raise WindowsVmError(reason)

        run_dir = Path(self.work_dir) / "runs" / _utc_stamp()
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        overlay = run_dir / "overlay.qcow2"
        self._create_overlay(overlay)

        port = self._free_port()
        session: Any = None
        process: Any = None
        report: dict | None = None
        skipped: list[str] = []

        try:
            process = self.popen(
                self.build_command(overlay, port, run_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            session = self._wait_for_winrm(port)
            skipped = self._upload(session, artifacts)
            self._upload_script(session)
            self._run_harness(session, timeout)
            report = self._read_report(session)
        finally:
            self._teardown(session, process, run_dir)

        if report is None:  # pragma: no cover - _read_report raises instead
            raise WindowsVmError("guest produced no report")
        return self._to_report(report, skipped)

    def _create_overlay(self, overlay: Path) -> None:
        qemu_img = self._resolved(self.qemu_img) or self.qemu_img
        completed = subprocess.run(
            [
                str(qemu_img),
                "create",
                "-f",
                "qcow2",
                "-b",
                str(self.base_image),
                "-F",
                "qcow2",
                str(overlay),
            ],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise WindowsVmError(f"qemu-img create failed: {completed.stderr.strip()}")

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    # ---- WinRM ----

    def _session(self, port: int, *, probe: bool = False) -> Any:
        if self.winrm_factory is not None:
            return self.winrm_factory(port)
        import winrm  # imported lazily: the extra is optional

        # pywinrm defaults to 20s per operation. The guest harness runs a
        # Defender scan and waits for launched artifacts, so a short timeout
        # closes the shell mid-run and looks like the guest died. During boot,
        # however, QEMU's hostfwd accepts the TCP connect before Windows
        # answers; a long read timeout would block the first probe for minutes.
        operation_timeout = 5.0 if probe else max(60.0, self.guest_timeout + 60.0)
        return winrm.Session(
            WINRM_URL.format(port=port),
            auth=(self.username, self.password),
            transport="ntlm",
            # pywinrm requires read_timeout_sec > operation_timeout_sec.
            read_timeout_sec=operation_timeout + (5.0 if probe else 30.0),
            operation_timeout_sec=operation_timeout,
        )

    def _wait_for_winrm(self, port: int) -> Any:
        deadline = time.monotonic() + self.boot_timeout
        last_error = "no attempt made"
        while time.monotonic() < deadline:
            try:
                session = self._session(port, probe=True)
                result = session.run_ps("$PSVersionTable.PSVersion.Major")
                if getattr(result, "status_code", 1) == 0:
                    # The probe must fail fast while boot is in progress; the
                    # harness itself needs the long operation timeout.
                    return self._session(port)
                last_error = getattr(result, "std_err", "unknown WinRM error")
            except Exception as exc:  # connection refused while Windows boots
                last_error = str(exc)
            time.sleep(3)
        raise WindowsVmError(
            f"guest did not answer WinRM within {self.boot_timeout:.0f}s: {last_error}"
        )

    def _ps(self, session: Any, script: str) -> Any:
        result = session.run_ps(script)
        if getattr(result, "status_code", 1) != 0:
            raise WindowsVmError(
                f"guest script failed: {getattr(result, 'std_err', 'no stderr')}".strip()
            )
        return result

    def _upload(self, session: Any, artifacts: list[Path]) -> list[str]:
        """Push artifacts into `C:\\triage\\input`; return what was skipped."""
        self._ps(
            session,
            f"New-Item -ItemType Directory -Force -Path '{GUEST_ROOT}\\input' | Out-Null",
        )
        skipped: list[str] = []
        total = 0
        for index, artifact in enumerate(artifacts):
            size = artifact.stat().st_size
            if size == 0:
                skipped.append(f"{artifact.name} (empty)")
                continue
            if size > self.max_artifact_bytes or total + size > self.max_artifact_bytes:
                skipped.append(f"{artifact.name} ({size} bytes)")
                continue
            name = _guest_name(index, artifact)
            self._write_remote(session, f"{GUEST_ROOT}\\input\\{name}", artifact.read_bytes())
            total += size
        return skipped

    def _write_remote(self, session: Any, windows_path: str, data: bytes) -> None:
        escaped = windows_path.replace("'", "''")
        for index, start in enumerate(range(0, len(data), CHUNK_BYTES)):
            chunk = base64.b64encode(data[start : start + CHUNK_BYTES]).decode("ascii")
            if index == 0:
                script = (
                    f"[IO.File]::WriteAllBytes('{escaped}', [Convert]::FromBase64String('{chunk}'))"
                )
            else:
                size = len(data[start : start + CHUNK_BYTES])
                script = (
                    f"$fs=[IO.File]::Open('{escaped}','Append'); "
                    f"$fs.Write([Convert]::FromBase64String('{chunk}'),0,{size}); "
                    "$fs.Close()"
                )
            self._ps(session, script)

    def _upload_script(self, session: Any) -> None:
        source = (
            importlib.resources.files("minus.triage.vm")
            .joinpath("guest_harness.ps1")
            .read_text(encoding="utf-8")
        )
        self._write_remote(session, f"{GUEST_ROOT}\\guest_harness.ps1", source.encode("utf-8"))

    def _run_harness(self, session: Any, timeout: float) -> None:
        # The default policy on a fresh guest blocks an unsigned script file.
        # Scope it to this process; changing the machine policy would be a
        # lasting security downgrade for the base image.
        self._ps(
            session,
            "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; "
            f"& {GUEST_ROOT}\\guest_harness.ps1 -TimeoutSeconds {max(1, int(timeout))}",
        )

    def _read_report(self, session: Any) -> dict:
        result = self._ps(
            session,
            f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{GUEST_ROOT}\\report.json'))",
        )
        encoded = getattr(result, "std_out", "")
        try:
            payload = json.loads(base64.b64decode(encoded))
        except Exception as exc:
            raise WindowsVmError(f"guest report was not readable JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WindowsVmError("guest report was not a JSON object")
        return payload

    # ---- Teardown and mapping ----

    def _teardown(self, session: Any, process: Any, run_dir: Path) -> None:
        if session is not None:
            try:
                session.run_ps("Stop-Computer -Force")
            except Exception:
                logger.debug("could not ask the guest to stop", exc_info=True)
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    logger.warning(
                        "qemu did not stop; leaking pid %s", getattr(process, "pid", "?")
                    )
        shutil.rmtree(run_dir, ignore_errors=True)

    def _to_report(self, report: dict, skipped: list[str]) -> SandboxReport:
        required = {
            "completed",
            "artifacts_launched",
            "defender",
            "processes",
            "network_endpoints",
            "file_writes",
            "registry_writes",
        }
        missing = required - set(report)
        if missing:
            raise WindowsVmError(f"guest report is missing {sorted(missing)}")

        notes = [str(report.get("notes") or "")]
        if skipped:
            notes.append("not staged: " + ", ".join(skipped))

        return SandboxReport(
            engine=self.name,
            detonated=bool(report["completed"]) and int(report["artifacts_launched"]) > 0,
            defender_detections=_strings(report["defender"]),
            processes=_strings(report["processes"]),
            network_endpoints=_strings(report["network_endpoints"]),
            file_writes=_strings(report["file_writes"]),
            registry_writes=_strings(report["registry_writes"]),
            notes="; ".join(part for part in notes if part),
        )


def runner_from_settings(settings: Any) -> WindowsVmRunner:
    """The runner the assembled assistant uses, from Settings.

    Kept here rather than in `assembly.py` so `minus vm-check` and the tools
    cannot describe two slightly different guests.
    """
    return WindowsVmRunner(
        qemu=Path(settings.windows_qemu),
        qemu_img=Path(shutil.which("qemu-img") or "qemu-img"),
        base_image=Path(settings.windows_base_image),
        work_dir=Path(settings.windows_work_dir),
        username=settings.windows_winrm_user,
        password=settings.windows_winrm_password,
        cpus=settings.windows_cpus,
        memory_mb=settings.windows_memory_mb,
        boot_timeout=settings.windows_boot_timeout,
        guest_timeout=settings.windows_guest_timeout,
        max_artifact_bytes=settings.windows_max_artifact_bytes,
        min_free_bytes=settings.windows_min_free_bytes,
    )


def _guest_name(index: int, artifact: Path) -> str:
    cleaned = _UNSAFE_NAME.sub("_", artifact.name).strip("._") or "artifact"
    return f"{index:02d}-{cleaned[:64]}"


def _strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise WindowsVmError("guest report field was not a list")
    return tuple(sorted({str(value) for value in values}))
