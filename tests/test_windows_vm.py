"""Sandbox pieces that can be exercised without a guest.

The qemu argv, the unattended-file substitution, the floppy image and the
installer argv are all real values produced by the shipping code; the parts that
need a Windows guest are not covered here at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from minus.triage import vm_build
from minus.triage.windows_vm import WindowsVmError, WindowsVmRunner


def test_the_command_has_no_guest_egress_and_a_host_only_forward(tmp_path):
    runner = WindowsVmRunner(qemu=Path("qemu-system-x86_64"))
    command = runner.build_command(tmp_path / "overlay.qcow2", 55985, tmp_path)
    joined = " ".join(command)

    assert command[0].endswith("qemu-system-x86_64")
    assert "-enable-kvm" in command
    assert "restrict=on" in joined
    assert "hostfwd=tcp:127.0.0.1:55985-:5985" in joined
    assert "overlay.qcow2" in joined and "if=ide" in joined
    assert "base.qcow2" not in joined
    # SeaBIOS + IDE + e1000 only: the guest installs and boots without OVMF,
    # a TPM, or any virtio driver.
    assert command[command.index("-machine") + 1] == "pc"
    assert "e1000,netdev=n0" in joined
    assert "q35" not in joined
    assert "if=virtio" not in joined
    assert "virtio-net-pci" not in joined


def test_the_unattend_password_is_substituted_once():
    rendered = vm_build.render_unattend("<Password>__WINRM_PASSWORD__</Password>", "s3cret")
    assert rendered == "<Password>s3cret</Password>"
    with pytest.raises(WindowsVmError):
        vm_build.render_unattend("<Password>__WINRM_PASSWORD__</Password>", "")


def test_a_floppy_is_a_real_fat_image_holding_the_unattend_file(tmp_path):
    for binary in ("mkfs.vfat", "mcopy"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} is not installed")

    source = tmp_path / "autounattend.xml"
    source.write_text("<unattend><Password>x</Password></unattend>")
    image = tmp_path / "autounattend.img"

    vm_build.build_floppy(source, image)

    assert image.stat().st_size > 0
    dumped = subprocess.run(
        ["mcopy", "-i", str(image), "::/autounattend.xml", "-"],
        capture_output=True,
        text=True,
    )
    if dumped.returncode != 0:
        dumped = subprocess.run(
            ["mtype", "-i", str(image), "::/autounattend.xml"], capture_output=True, text=True
        )
    assert "<Password>x</Password>" in dumped.stdout


def test_remote_upload_scripts_stay_inside_the_windows_command_line_limit():
    class FakeSession:
        def __init__(self) -> None:
            self.scripts: list[str] = []

        def run_ps(self, script: str):
            self.scripts.append(script)
            return type("Result", (), {"status_code": 0, "std_err": ""})()

    session = FakeSession()
    WindowsVmRunner()._write_remote(session, "C:\\triage\\payload.bin", b"a" * 5000)

    assert len(session.scripts) > 1
    assert all(len(script) < 8000 for script in session.scripts)


def test_version_metadata_decodes_pywinrm_bytes():
    assert vm_build._text(b"Windows 10 Enterprise LTSC Evaluation\r\n") == (
        "Windows 10 Enterprise LTSC Evaluation"
    )
    assert vm_build._text("already text") == "already text"
    assert vm_build._text(None) == ""


def test_the_installer_argv_mounts_both_isos_and_the_floppy(tmp_path):
    command = vm_build.install_qemu(
        Path("qemu"),
        tmp_path / "base.qcow2",
        tmp_path / "win.iso",
        tmp_path / "attend.img",
        55985,
        virtio_iso=tmp_path / "virtio.iso",
    )
    joined = " ".join(command)

    assert "-enable-kvm" in command
    assert command[command.index("-machine") + 1] == "pc"
    assert joined.count("media=cdrom") == 2
    assert "if=ide" in joined
    assert "e1000,netdev=n0" in joined
    assert "if=floppy" in joined
    assert "restrict=on" in joined
    assert "base.qcow2" in joined
    assert "monitor.sock" in joined


def test_the_installer_argv_allows_a_missing_virtio_iso(tmp_path):
    command = vm_build.install_qemu(
        Path("qemu"),
        tmp_path / "base.qcow2",
        tmp_path / "win.iso",
        tmp_path / "attend.img",
        55985,
        virtio_iso=None,
    )
    joined = " ".join(command)

    # The BIOS/IDE guest needs no driver ISO, so only the Windows media is a CD.
    assert joined.count("media=cdrom") == 1
    assert "virtio.iso" not in joined
