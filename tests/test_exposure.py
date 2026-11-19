"""Listening-socket exposure, parsed from real `ss` output and real sockets.

The parser is fed a literal table so the address forms are exercised cheaply;
the live test then binds both a wildcard and a loopback socket in this process
and asks the kernel through `ss` what it sees, which is the same path the
doctor report uses.
"""

from __future__ import annotations

import shutil
import socket

import pytest

import minus.doctor as doctor
from minus.config import Settings
from minus.services import exposure

_SS_TABLE = """\
State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      4096   0.0.0.0:8788       0.0.0.0:*
LISTEN 0      4096   127.0.0.1:11434    0.0.0.0:*
LISTEN 0      4096   [::]:22            [::]:*
LISTEN 0      4096   [::1]:44481        [::]:*
LISTEN 0      4096   *:9000             *:*
ESTAB  0      0      127.0.0.1:9999     127.0.0.1:50000
"""


def test_parse_ss_marks_only_loopback_private():
    parsed = exposure.parse_ss(_SS_TABLE)

    assert [entry["port"] for entry in parsed] == [22, 8788, 9000, 11434, 44481]
    by_port = {entry["port"]: entry for entry in parsed}
    assert by_port[22] == {"address": "::", "port": 22, "public": True}
    assert by_port[9000] == {"address": "*", "port": 9000, "public": True}
    assert by_port[8788] == {"address": "0.0.0.0", "port": 8788, "public": True}
    assert by_port[11434] == {"address": "127.0.0.1", "port": 11434, "public": False}
    assert by_port[44481] == {"address": "::1", "port": 44481, "public": False}


def test_parse_ss_accepts_headerless_output():
    parsed = exposure.parse_ss("LISTEN 0 4096 127.0.0.1:5000 0.0.0.0:*\n")

    assert parsed == [{"address": "127.0.0.1", "port": 5000, "public": False}]


def test_real_sockets_are_reported_public_only_for_the_wildcard():
    if shutil.which("ss") is None:
        pytest.skip("ss is not installed")

    wildcard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    loopback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        wildcard.bind(("0.0.0.0", 0))
        wildcard.listen(1)
        loopback.bind(("127.0.0.1", 0))
        loopback.listen(1)

        wildcard_port = wildcard.getsockname()[1]
        loopback_port = loopback.getsockname()[1]

        by_port = {entry["port"]: entry for entry in exposure.listeners()}

        assert by_port[wildcard_port]["public"] is True
        assert by_port[loopback_port]["public"] is False
        assert by_port[wildcard_port]["address"] == "0.0.0.0"
        assert by_port[loopback_port]["address"] == "127.0.0.1"
    finally:
        wildcard.close()
        loopback.close()


def test_doctor_names_public_ports_and_renders_them(monkeypatch, tmp_path):
    entry = {"address": "0.0.0.0", "port": 9000, "public": True}
    monkeypatch.setattr(doctor.exposure, "public_listeners", lambda: [entry])

    posture = doctor.collect_posture(Settings(), socket_path=tmp_path / "control.sock")

    assert posture["listeners"]["public"] == [entry]
    sentence = next(
        finding for finding in posture["dangerous"] if "public listening socket" in finding
    )
    assert "ports 9000" in sentence
    assert "bind them to 127.0.0.1" in sentence

    report = doctor.format_posture(posture)
    assert "listeners:" in report
    assert "0.0.0.0:9000" in report


def test_doctor_says_none_public_when_socket_list_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.exposure, "public_listeners", lambda: [])

    posture = doctor.collect_posture(Settings(), socket_path=tmp_path / "control.sock")

    assert posture["listeners"]["public"] == []
    report = doctor.format_posture(posture)
    assert "none public" in report
    assert not any("public listening socket" in finding for finding in posture["dangerous"])
