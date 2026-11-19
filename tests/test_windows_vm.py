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
    assert command[command.index("-boot") + 1] == "order=c"
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


def test_skip_reason_needs_a_launchable_artifact(tmp_path):
    runner = WindowsVmRunner()

    movie = tmp_path / "clip.mp4"
    movie.write_bytes(b"\0" * (2 * 1024**2))
    assert runner.skip_reason([movie])

    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ" + b"\0" * 1022)
    assert runner.skip_reason([installer]) == ""


def test_skip_reason_rejects_a_launchable_artifact_over_the_cap(tmp_path):
    runner = WindowsVmRunner(max_artifact_bytes=16)
    oversized = tmp_path / "big.exe"
    oversized.write_bytes(b"MZ" + b"\0" * 20)

    assert runner.skip_reason([oversized])


def test_skip_reason_is_safe_on_empty_and_missing_artifacts(tmp_path):
    runner = WindowsVmRunner()

    assert runner.skip_reason([])
    assert runner.skip_reason([tmp_path / "gone.exe"])


def test_a_failed_run_keeps_its_evidence(tmp_path):
    work_dir = tmp_path / "work"
    run_dir = work_dir / "runs" / "20260101T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "console.log").write_text("SeaBIOS booting\n")

    runner = WindowsVmRunner(work_dir=work_dir)
    runner._teardown(None, None, run_dir, keep=True, error=RuntimeError("boom"))

    kept = work_dir / "failed" / run_dir.name
    assert (kept / "failure.txt").read_text() == "RuntimeError: boom"
    assert (kept / "console.log").read_text() == "SeaBIOS booting\n"
    assert (kept / "failure.txt").stat().st_mode & 0o777 == 0o600
    assert not run_dir.exists()


def test_a_successful_run_removes_its_directory(tmp_path):
    work_dir = tmp_path / "work"
    run_dir = work_dir / "runs" / "20260101T000001Z"
    run_dir.mkdir(parents=True)
    (run_dir / "console.log").write_text("done\n")

    runner = WindowsVmRunner(work_dir=work_dir)
    runner._teardown(None, None, run_dir, keep=False)

    assert not run_dir.exists()
    assert not (work_dir / "failed").exists()


def test_review_mode_attaches_a_read_only_cd(tmp_path):
    runner = WindowsVmRunner(qemu=Path("qemu-system-x86_64"))
    command = runner.build_command(
        tmp_path / "overlay.qcow2",
        55985,
        tmp_path,
        review_iso=tmp_path / "review.iso",
    )
    joined = " ".join(command)

    assert "review.iso" in joined
    assert "media=cdrom" in joined
    assert "readonly=on" in joined
    # The OS disk is still the first IDE drive; the CD is additive.
    assert command[command.index("-drive") + 1].startswith("file=")
    assert "overlay.qcow2" in command[command.index("-drive") + 1]


def test_review_iso_carries_the_sanitized_artifact_names(tmp_path):
    import pycdlib

    movie = tmp_path / "Some Movie.mp4"
    movie.write_bytes(b"video-bytes")
    runner = WindowsVmRunner(min_free_bytes=0)

    iso_path = runner._build_review_iso([movie], tmp_path)

    image = pycdlib.PyCdlib()
    try:
        image.open(str(iso_path))
        extracted = tmp_path / "extracted.mp4"
        image.get_file_from_iso(str(extracted), joliet_path="/00-Some_Movie.mp4")
        assert extracted.read_bytes() == b"video-bytes"
    finally:
        image.close()


def test_opened_media_counts_as_a_detonation_and_reports_graveyard():
    report = {
        "completed": True,
        "artifacts_launched": 0,
        "artifacts_opened": 1,
        "artifacts_graveyarded": 2,
        "defender": [],
        "processes": [],
        "network_endpoints": [],
        "file_writes": [],
        "registry_writes": [],
        "notes": "",
    }

    result = WindowsVmRunner()._to_report(report, [], review=True)

    assert result.detonated is True
    assert result.opened == 1
    assert "graveyard: 2 file(s)" in result.notes


class RecordingSession:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def run_ps(self, script: str):
        self.scripts.append(script)
        return type("Result", (), {"status_code": 0, "std_err": "", "std_out": ""})()


def test_upload_script_creates_the_guest_root_first():
    session = RecordingSession()
    WindowsVmRunner()._upload_script(session)

    assert "New-Item -ItemType Directory -Force -Path 'C:\\triage'" in session.scripts[0]


def test_keep_list_is_written_to_a_file_not_the_command_line():
    session = RecordingSession()
    WindowsVmRunner()._run_harness(session, 10, review=True, keep={"00-movie.mp4", "01-subs.srt"})

    assert any("C:\\triage\\keep.txt" in script for script in session.scripts)
    assert "-KeepFile 'C:\\triage\\keep.txt'" in session.scripts[-1]
    assert "-Keep '" not in session.scripts[-1]


def test_a_partial_review_is_not_a_detonation():
    report = {
        "completed": False,
        "artifacts_launched": 0,
        "artifacts_opened": 1,
        "defender": [],
        "processes": [],
        "network_endpoints": [],
        "file_writes": [],
        "registry_writes": [],
        "notes": "partial copy",
    }

    result = WindowsVmRunner()._to_report(report, [], review=True)

    assert result.completed is False
    assert result.detonated is False


def test_failed_review_drops_the_iso_and_keeps_private_evidence(tmp_path):
    work = tmp_path / "work"
    run_dir = work / "runs" / "20260101T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "review.iso").write_bytes(b"iso")
    (run_dir / "overlay.qcow2").write_bytes(b"overlay")
    (run_dir / "console.log").write_text("boot\n")

    runner = WindowsVmRunner(work_dir=work)
    runner._retain(run_dir, RuntimeError("boom"))

    kept = work / "failed" / run_dir.name
    assert not (run_dir / "review.iso").exists()
    assert not (kept / "review.iso").exists()
    assert (kept / "overlay.qcow2").is_file()
    assert (kept.stat().st_mode & 0o777) == 0o700
    assert ((kept / "overlay.qcow2").stat().st_mode & 0o777) == 0o600


def test_the_installer_can_attach_a_tools_iso(tmp_path):
    command = vm_build.install_qemu(
        Path("qemu"),
        tmp_path / "base.qcow2",
        tmp_path / "win.iso",
        tmp_path / "attend.img",
        55985,
        tools_iso=tmp_path / "tools.iso",
    )
    joined = " ".join(command)

    assert "tools.iso" in joined
    assert joined.count("media=cdrom") == 2
