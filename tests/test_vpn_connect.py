"""AirVPN connection management, entirely offline.

Every client invocation is a fake executable in `tmp_path` that records its
argv and exits with a scripted status. The routing preflight is monkeypatched
where the test is not about routing, so no interface, tunnel or network is
touched.
"""

from __future__ import annotations

import json
import stat
import subprocess

import pytest

from minus.config import Settings
from minus.errors import ToolExecutionError
from minus.services import vpn
from minus.services.vpn import TunnelStatus, VpnError
from minus.tools.vpn import VpnTools


def _script(tmp_path, body: str, name: str = "fake-goldcrest"):
    """A fake goldcrest that logs JSON argv lines to a sibling file."""
    log = tmp_path / (name + ".log")
    path = tmp_path / name
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n" + body
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, log


def _recorded(log):
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def _settings(script, **overrides):
    values = {
        "vpn_client_command": str(script),
        "airvpn_username": "user@example.test",
        "airvpn_password": "s3cret-pass",
        "airvpn_server": "",
        "airvpn_country": "",
        "airvpn_vpn_type": "wireguard",
        "airvpn_network_lock": True,
        "vpn_connect_timeout": 0.0,
        "vpn_use_sudo_fallback": True,
    }
    values.update(overrides)
    return Settings(**values)


HEALTHY = TunnelStatus("tun0", True, True, "traffic leaves through tun0")


def _healthy(_settings):
    return HEALTHY


def test_connect_builds_server_argv_and_returns_the_tunnel(tmp_path, monkeypatch):
    script, log = _script(tmp_path, "sys.exit(0)\n")
    settings = _settings(script, airvpn_server="earth")
    monkeypatch.setattr(vpn, "tunnel_status", _healthy)

    snapshot = vpn.connect(settings)

    assert snapshot is HEALTHY
    argv = _recorded(log)[0]
    assert argv[0:2] == ["--air-connect", "--air-user"]
    assert "user@example.test" in argv
    assert "--air-password" in argv
    assert argv[argv.index("--air-password") + 1] == "s3cret-pass"
    assert argv[argv.index("--air-server") + 1] == "earth"
    assert "--air-country" not in argv
    assert argv[argv.index("--air-vpn-type") + 1] == "wireguard"
    assert argv[argv.index("--network-lock") + 1] == "on"
    assert argv[argv.index("--timeout") + 1] == "0"


def test_connect_uses_country_and_network_lock_off(tmp_path, monkeypatch):
    script, log = _script(tmp_path, "sys.exit(0)\n")
    settings = _settings(
        script,
        airvpn_country="DE",
        airvpn_vpn_type="openvpn",
        airvpn_network_lock=False,
    )
    monkeypatch.setattr(vpn, "tunnel_status", _healthy)

    vpn.connect(settings)

    argv = _recorded(log)[0]
    assert argv[argv.index("--air-country") + 1] == "DE"
    assert "--air-server" not in argv
    assert argv[argv.index("--air-vpn-type") + 1] == "openvpn"
    assert argv[argv.index("--network-lock") + 1] == "off"


def test_empty_credentials_are_refused_without_running_the_client(tmp_path):
    script, log = _script(tmp_path, "sys.exit(0)\n")
    settings = _settings(script, airvpn_username="", airvpn_password="")

    with pytest.raises(VpnError) as caught:
        vpn.connect(settings)

    assert "credentials" in str(caught.value)
    assert not log.exists()


def test_connect_failure_raises_and_never_echoes_the_password(tmp_path):
    script, _log = _script(
        tmp_path,
        "print('authentication rejected for password ' + sys.argv[5])\nsys.exit(3)\n",
        name="failing-goldcrest",
    )
    settings = _settings(script)

    with pytest.raises(VpnError) as caught:
        vpn.connect(settings)

    message = str(caught.value)
    assert "AirVPN connect failed" in message
    assert "authentication rejected" in message
    assert "s3cret-pass" not in message


def test_login_failure_exiting_zero_fails_fast(tmp_path):
    script, _log = _script(
        tmp_path,
        "print('ERROR: AirVPN login failed for user user@example.test')\nsys.exit(0)\n",
        name="login-failing-goldcrest",
    )

    with pytest.raises(VpnError) as caught:
        vpn.connect(_settings(script))

    assert "AirVPN login failed" in str(caught.value)
    assert "s3cret-pass" not in str(caught.value)


def test_a_missing_wireguard_module_fails_fast(tmp_path):
    script, _log = _script(
        tmp_path,
        "print('ERROR: cannot load wireguard system module')\nsys.exit(0)\n",
        name="no-wireguard-goldcrest",
    )

    with pytest.raises(VpnError) as caught:
        vpn.connect(_settings(script))

    assert "transport" in str(caught.value)
    assert "openvpn" in str(caught.value)


def test_connect_times_out_when_the_client_succeeds_but_no_tunnel_appears(tmp_path, monkeypatch):
    script, _log = _script(tmp_path, "sys.exit(0)\n")
    settings = _settings(script)
    monkeypatch.setattr(
        vpn,
        "tunnel_status",
        lambda _settings: TunnelStatus("tun0", True, False, "route still on eth0"),
    )

    with pytest.raises(VpnError) as caught:
        vpn.connect(settings)

    assert "route still on eth0" in str(caught.value)


def test_disconnect_reports_exit_and_output(tmp_path):
    script, log = _script(tmp_path, "print('Disconnected from Earth')\n")
    settings = _settings(script)

    result = vpn.disconnect(settings)

    assert result["disconnected"] is True
    assert "Disconnected from Earth" in result["output"]
    assert _recorded(log)[0] == ["--disconnect"]


def test_disconnect_reports_a_non_zero_exit(tmp_path):
    script, _log = _script(tmp_path, "print('not running')\nsys.exit(1)\n")
    result = vpn.disconnect(_settings(script))
    assert result["disconnected"] is False


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Bluetit is connected to Earth\n", True),
        ("Bluetit is not connected\n", False),
        ("CONNECTED\n", True),
        ("nothing useful\n", False),
    ],
)
def test_client_status_parses_connected_marker(tmp_path, output, expected):
    script, _log = _script(tmp_path, f"print({output!r})\n")
    assert vpn.client_is_connected(_settings(script)) is expected


def test_a_failing_status_command_is_treated_as_not_connected(tmp_path):
    script, _log = _script(tmp_path, "print('connected')\nsys.exit(2)\n")
    assert vpn.client_is_connected(_settings(script)) is False


def test_missing_command_is_treated_as_not_connected(tmp_path):
    settings = Settings(vpn_client_command=str(tmp_path / "no-such-goldcrest"))
    assert vpn.client_is_connected(settings) is False


def test_sudo_fallback_is_attempted_only_for_a_policy_refusal(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                argv, 1, "", "GDBus.Error: D-Bus policy refused the name\n"
            )
        return subprocess.CompletedProcess(argv, 0, "connected\n", "")

    monkeypatch.setattr(vpn.subprocess, "run", fake_run)
    monkeypatch.setattr(vpn.shutil, "which", lambda name: f"/usr/bin/{name}")
    settings = Settings(vpn_use_sudo_fallback=True)

    completed = vpn._run_client(["goldcrest", "--bluetit-status"], 5, settings)

    assert completed.returncode == 0
    assert len(calls) == 2
    assert calls[1] == ["sudo", "-n", "/usr/bin/goldcrest", "--bluetit-status"]


def test_no_sudo_fallback_for_an_ordinary_failure(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "connection timed out\n")

    monkeypatch.setattr(vpn.subprocess, "run", fake_run)
    monkeypatch.setattr(vpn.shutil, "which", lambda name: f"/usr/bin/{name}")
    settings = Settings(vpn_use_sudo_fallback=True)

    completed = vpn._run_client(["goldcrest", "--air-connect"], 5, settings)

    assert completed.returncode == 1
    assert len(calls) == 1


def test_sudo_fallback_can_be_disabled(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "D-Bus policy refused\n")

    monkeypatch.setattr(vpn.subprocess, "run", fake_run)
    monkeypatch.setattr(vpn.shutil, "which", lambda name: f"/usr/bin/{name}")
    settings = Settings(vpn_use_sudo_fallback=False)

    vpn._run_client(["goldcrest", "--bluetit-status"], 5, settings)

    assert len(calls) == 1


class _Registry:
    def __init__(self):
        self.registered = []

    def tool(self, tool, category=None):
        self.registered.append((tool, category))


def test_tools_register_under_the_vpn_category():
    registry = _Registry()
    VpnTools(Settings()).register(registry)

    assert [tool.__name__ for tool, _category in registry.registered] == [
        "vpn_status",
        "vpn_connect",
        "vpn_disconnect",
    ]
    assert {category for _tool, category in registry.registered} == {"vpn"}


def test_bind_downloads_persists_the_interface_and_applies_it(tmp_path, monkeypatch):
    written: dict[str, str] = {}
    applied: dict[str, str] = {}

    def fake_update(_path, updates, allowed=None):
        written.update(updates)
        return list(updates)

    class FakeClient:
        def close(self) -> None:
            pass

    monkeypatch.setattr("minus.services.env_file.update_env_file", fake_update)
    monkeypatch.setattr("minus.paths.project_root", lambda: tmp_path)
    monkeypatch.setattr("minus.scripts.qbt.ensure_running", lambda _settings: FakeClient())
    monkeypatch.setattr(
        "minus.scripts.qbt.apply_preferences",
        lambda _client, _settings, interface: applied.setdefault("interface", interface),
    )
    settings = Settings(
        vpn_interface="",
        qbittorrent_vpn_interface="",
        qbittorrent_url="http://127.0.0.1:8080",
    )

    result = vpn.bind_downloads(settings, HEALTHY)

    assert result == {"bound": True, "interface": "tun0"}
    assert written == {
        "MINUS_VPN_INTERFACE": "tun0",
        "MINUS_QBITTORRENT_VPN_INTERFACE": "tun0",
    }
    assert applied["interface"] == "tun0"
    assert settings.vpn_interface == "tun0"
    assert settings.qbittorrent_vpn_interface == "tun0"


def test_bind_downloads_without_qbittorrent_reports_why(tmp_path, monkeypatch):
    monkeypatch.setattr("minus.services.env_file.update_env_file", lambda *a, **k: [])
    monkeypatch.setattr("minus.paths.project_root", lambda: tmp_path)
    settings = Settings(
        vpn_interface="",
        qbittorrent_vpn_interface="",
        qbittorrent_url="",
    )

    result = vpn.bind_downloads(settings, HEALTHY)

    assert result["bound"] is False
    assert "not configured" in result["reason"]


def test_bind_downloads_reports_a_failed_bind(tmp_path, monkeypatch):
    monkeypatch.setattr("minus.services.env_file.update_env_file", lambda *a, **k: [])
    monkeypatch.setattr("minus.paths.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "minus.scripts.qbt.ensure_running",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("webui down")),
    )
    settings = Settings(
        vpn_interface="tun0",
        qbittorrent_vpn_interface="tun0",
        qbittorrent_url="http://127.0.0.1:8080",
    )

    result = vpn.bind_downloads(settings, HEALTHY)

    assert result["bound"] is False
    assert "webui down" in result["error"]


def test_vpn_status_reports_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(vpn, "tunnel_status", _healthy)
    result = VpnTools(Settings(vpn_provider="airvpn")).vpn_status()
    assert result["interface"] == "tun0"
    assert result["up"] is True
    assert result["carrying"] is True
    assert result["provider"] == "airvpn"


def test_vpn_connect_does_not_spawn_a_second_connect_when_healthy(monkeypatch):
    monkeypatch.setattr(vpn, "tunnel_status", _healthy)

    def boom(_settings):
        raise AssertionError("connect must not run against a healthy tunnel")

    monkeypatch.setattr(vpn, "connect", boom)

    message = VpnTools(Settings()).vpn_connect()

    assert "already up" in message


def test_tool_errors_become_tool_execution_errors(monkeypatch):
    def boom(_settings):
        raise VpnError("AirVPN refused")

    monkeypatch.setattr(vpn, "connect", boom)
    monkeypatch.setattr(
        vpn,
        "tunnel_status",
        lambda _settings: TunnelStatus("", False, False, "no tunnel"),
    )

    with pytest.raises(ToolExecutionError):
        VpnTools(Settings()).vpn_connect()
