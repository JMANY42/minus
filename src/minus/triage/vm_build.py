"""Build the sandbox base image once, then never write to it again.

A Windows install is not something to repeat per run: the base qcow2 is created
here, every detonation uses a throwaway overlay on top of it, and a failed build
deletes the partial image so it cannot be mistaken for a usable one.

Requirements this cannot verify or install: qemu (`qemu-full`), a Windows x64
ISO, and a work directory with room for a 64 GiB sparse image. The guest
installs under SeaBIOS with an IDE disk and the e1000 NIC, both of which
Windows Setup already knows, so a virtio-win ISO is optional and only attached
when a caller supplies one. The unattended file is written with the WinRM
password substituted and is not logged.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from minus.triage.windows_vm import WindowsVmError, WindowsVmRunner, _which

logger = logging.getLogger(__name__)

FLOPPY_KB = 1440
BASE_IMAGE_SIZE = "64G"
INSTALL_TIMEOUT = 2400.0  # 40 minutes: a Windows install on spinning disk is slow
PASSWORD_TOKEN = "__WINRM_PASSWORD__"


def render_unattend(template: str, password: str) -> str:
    """Substitute the WinRM password into the unattended template."""
    if not password:
        raise WindowsVmError(
            "MINUS_WINDOWS_WINRM_PASSWORD is not set; the guest needs an account password"
        )
    if PASSWORD_TOKEN not in template:
        raise WindowsVmError(f"unattend template has no {PASSWORD_TOKEN} token")
    # The password lands inside XML attribute/character data: escape it so a
    # quote or ampersand cannot break the document or inject a command.
    quoted = escape(password, {'"': "&quot;", "'": "&apos;"})
    return template.replace(PASSWORD_TOKEN, quoted)


def build_floppy(autounattend: Path, image: Path) -> None:
    """A 1.44 MB FAT image holding `autounattend.xml` — what Setup looks for.

    mtools (already present on this machine) does this without an ISO tool:
    `mkfs.vfat -C` creates the filesystem, `mcopy` puts the file at the root.
    """
    image.unlink(missing_ok=True)
    formatted = subprocess.run(
        ["mkfs.vfat", "-C", str(image), str(FLOPPY_KB)],
        capture_output=True,
        text=True,
    )
    if formatted.returncode != 0:
        raise WindowsVmError(f"mkfs.vfat failed: {formatted.stderr.strip()}")

    copied = subprocess.run(
        ["mcopy", "-i", str(image), str(autounattend), "::/autounattend.xml"],
        capture_output=True,
        text=True,
    )
    if copied.returncode != 0:
        raise WindowsVmError(f"mcopy failed: {copied.stderr.strip()}")


def require_tools() -> None:
    missing = [binary for binary in ("mkfs.vfat", "mcopy") if shutil.which(binary) is None]
    if missing:
        raise WindowsVmError(
            "missing " + ", ".join(missing) + " (pacman -S --needed mtools dosfstools)"
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _text(value: object) -> str:
    """Pywinrm reports stdout as bytes; metadata and logs want text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value or "").strip()


def _qemu_version(qemu: Path) -> str:
    try:
        completed = subprocess.run(
            [str(qemu), "--version"], capture_output=True, text=True, timeout=10
        )
        return completed.stdout.splitlines()[0] if completed.stdout else "unknown"
    except Exception:  # pragma: no cover - cosmetic metadata
        return "unknown"


def install_qemu(
    qemu: Path,
    base: Path,
    iso: Path,
    floppy: Path,
    port: int,
    virtio_iso: Path | None = None,
    tools_iso: Path | None = None,
) -> list[str]:
    """The installer's argv. Separate so the test can assert it.

    SeaBIOS with an IDE disk and the e1000 NIC: Windows Setup carries drivers
    for both, so the install needs neither OVMF nor a virtio-win ISO. The
    virtio ISO is attached only when a caller supplies one.
    """
    command = [
        str(qemu),
        "-enable-kvm",
        "-machine",
        "pc",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "4096",
        "-drive",
        f"file={base},if=ide,format=qcow2",
        "-drive",
        f"file={iso},media=cdrom,readonly=on",
    ]
    if virtio_iso is not None:
        command += ["-drive", f"file={virtio_iso},media=cdrom,readonly=on"]
    if tools_iso is not None:
        command += ["-drive", f"file={tools_iso},media=cdrom,readonly=on"]
    command += [
        "-drive",
        f"file={floppy},if=floppy",
        "-netdev",
        f"user,id=n0,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:5985",
        "-device",
        "e1000,netdev=n0",
        "-display",
        "none",
        "-serial",
        f"file:{base.parent / 'install-console.log'}",
        # Windows bootmgr prints "Press any key to boot from CD" when it sees
        # an attached disk. With -display none nobody can press it, so the
        # build sends Enter through this monitor socket.
        "-monitor",
        f"unix:{base.parent / 'monitor.sock'},server,nowait",
        # Setup reboots into the installed disk; -no-reboot would kill QEMU
        # before WinRM ever came up. Boot the CD once, then the disk.
        "-boot",
        "once=d",
    ]
    return command


def _press_boot_key(monitor: Path, *, seconds: float = 12.0) -> None:
    """Send Enter to the QEMU monitor while bootmgr's CD prompt is open.

    The prompt is only a few seconds; repeated keys are harmless once Windows
    Setup owns the screen. A missing monitor is not fatal: the build will time
    out with a useful WinRM error rather than fail here.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(monitor))
                client.sendall(b"sendkey ret\n")
        except OSError:
            pass
        time.sleep(0.75)


def _tool_iso(zip_path: Path, destination: Path) -> Path:
    """A tiny read-only CD carrying one tool zip, labelled MINUS_TOOLS.

    Uploading a 79 MB VLC portable through WinRM would be ~40,000 two-kilobyte
    calls; a second CD is the same one-way channel review mode already uses.
    """
    try:
        import pycdlib
    except ImportError as exc:  # pragma: no cover - windows extra is required
        raise WindowsVmError(
            "pycdlib is required to attach a tools ISO (uv sync --extra windows)"
        ) from exc
    iso = pycdlib.PyCdlib()
    try:
        iso.new(joliet=3, vol_ident="MINUS_TOOLS")
        iso.add_file(str(zip_path), iso_path="/VLC_ZIP", joliet_path="/vlc.zip")
        iso.write(str(destination))
    finally:
        iso.close()
    return destination


def build_base_image(
    settings,
    *,
    iso: Path,
    work_dir: Path,
    virtio_iso: Path | None = None,
    sysmon_zip: Path | None = None,
    vlc_zip: Path | None = None,
) -> Path:
    """Create `work_dir/base.qcow2` and return it."""
    require_tools()
    if not iso.is_file():
        raise WindowsVmError(f"Windows ISO not found: {iso}")
    if virtio_iso is not None and not virtio_iso.is_file():
        raise WindowsVmError(f"virtio-win ISO not found: {virtio_iso}")
    if sysmon_zip is not None and not sysmon_zip.is_file():
        raise WindowsVmError(f"Sysmon zip not found: {sysmon_zip}")
    if vlc_zip is not None and not vlc_zip.is_file():
        raise WindowsVmError(f"VLC zip not found: {vlc_zip}")

    qemu = _which(settings.windows_qemu)
    qemu_img = _which("qemu-img")
    if qemu is None or qemu_img is None:
        raise WindowsVmError("qemu-full is not installed (qemu-system-x86_64 / qemu-img)")

    password = settings.windows_winrm_password
    work_dir.mkdir(parents=True, exist_ok=True)
    base = work_dir / "base.qcow2"
    if base.exists():
        raise WindowsVmError(f"{base} already exists; remove it to rebuild the image")

    template = (
        importlib.resources.files("minus.triage.vm")
        .joinpath("autounattend.xml")
        .read_text(encoding="utf-8")
    )
    autounattend = work_dir / "autounattend.xml"
    # 0600, and removed in the finally below: this file is the guest account's
    # password in plain text and has no reason to outlive the install.
    descriptor = os.open(
        autounattend, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(render_unattend(template, password))
    floppy = work_dir / "autounattend.img"
    build_floppy(autounattend, floppy)
    try:
        tools_iso = _tool_iso(vlc_zip, work_dir / "tools.iso") if vlc_zip is not None else None
    except BaseException:
        autounattend.unlink(missing_ok=True)
        floppy.unlink(missing_ok=True)
        raise

    created = subprocess.run(
        [str(qemu_img), "create", "-f", "qcow2", str(base), BASE_IMAGE_SIZE],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        autounattend.unlink(missing_ok=True)
        floppy.unlink(missing_ok=True)
        if tools_iso is not None:
            tools_iso.unlink(missing_ok=True)
        raise WindowsVmError(f"qemu-img create failed: {created.stderr.strip()}")

    port = _free_port()
    monitor = base.parent / "monitor.sock"
    monitor.unlink(missing_ok=True)
    process = subprocess.Popen(
        install_qemu(qemu, base, iso, floppy, port, virtio_iso=virtio_iso, tools_iso=tools_iso),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _press_boot_key(monitor)
    runner = WindowsVmRunner(
        qemu=qemu,
        qemu_img=qemu_img,
        base_image=base,
        work_dir=work_dir,
        username=settings.windows_winrm_user,
        password=password,
        boot_timeout=INSTALL_TIMEOUT,
    )
    version = "unknown"
    try:
        # Deliberately reusing the runner's transport helpers: there is one
        # WinRM implementation in this package and a second one would drift.
        session = runner._wait_for_winrm(port)
        if sysmon_zip is not None:
            runner._ps(session, "New-Item -ItemType Directory -Force -Path 'C:\\triage' | Out-Null")
            runner._write_remote(session, "C:\\triage\\sysmon.zip", sysmon_zip.read_bytes())
            runner._ps(
                session,
                "$z='C:\\triage\\sysmon.zip'; "
                "Expand-Archive -Path $z -DestinationPath 'C:\\triage\\sysmon' -Force; "
                "& 'C:\\triage\\sysmon\\Sysmon64.exe' -accepteula -i | Out-Null",
            )
        if vlc_zip is not None:
            # VLC portable is the reliable media parser: Windows Media Player's
            # first-run setup can exit instead of opening the file. It arrives
            # on the tools CD, not through 40,000 WinRM chunks.
            runner._ps(
                session,
                "New-Item -ItemType Directory -Force -Path 'C:\\tools' | Out-Null; "
                "$cd = Get-CimInstance Win32_LogicalDisk | "
                "Where-Object { $_.VolumeName -eq 'MINUS_TOOLS' } | Select-Object -First 1; "
                "if ($null -eq $cd) { throw 'MINUS_TOOLS CD not found' }; "
                "Copy-Item -Path \"$($cd.DeviceID)\\*\" -Destination 'C:\\tools' -Force; "
                "$z = Get-ChildItem -Path 'C:\\tools' -Filter '*.zip' | Select-Object -First 1; "
                "Expand-Archive -Path $z.FullName -DestinationPath 'C:\\tools\\vlc' -Force",
            )
        version = (
            _text(runner._ps(session, "(Get-CimInstance Win32_OperatingSystem).Caption").std_out)
            or "unknown"
        )
        runner._ps(session, "Stop-Computer -Force")
        process.wait(timeout=180)
    except Exception as exc:
        base.unlink(missing_ok=True)
        raise WindowsVmError(f"base image build failed: {exc}") from exc
    finally:
        autounattend.unlink(missing_ok=True)
        monitor.unlink(missing_ok=True)
        if tools_iso is not None:
            tools_iso.unlink(missing_ok=True)
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=60)
            except Exception:  # pragma: no cover - wedged qemu
                process.kill()

    meta = {
        "windows_version": version,
        "qemu_version": _qemu_version(qemu),
        "sysmon": sysmon_zip is not None,
        "vlc": vlc_zip is not None,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (work_dir / "base.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    floppy.unlink(missing_ok=True)
    return base
