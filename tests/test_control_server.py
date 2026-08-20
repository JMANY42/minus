"""The control socket, exercised over a real socket with a real client.

A fake transport would prove the dispatch table works and nothing about the
framing, the threading, or what happens when a peer stops reading -- which is
where the interesting failures are. These use AF_UNIX in a temp directory,
which costs milliseconds.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from minus.control import protocol
from minus.control.client import ControlClient, ControlError, NotRunning
from minus.control.server import ControlServer, ControlServerBusy
from minus.control.state import THINKING, RuntimeState


@pytest.fixture
def spoken():
    return []


@pytest.fixture
def server(short_socket_path, spoken):
    state = RuntimeState(pid=4242)
    handlers = {
        "ping": lambda params: {"pong": True},
        "say": lambda params: _say(spoken, params),
        "get_status": lambda params: state.snapshot(),
        "boom": lambda params: 1 / 0,
    }
    server = ControlServer(short_socket_path, handlers, state=state)
    server.state_for_test = state
    server.start()
    yield server
    server.close()


def _say(spoken: list[str], params: dict) -> dict:
    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        raise protocol.ProtocolError("text must be a non-empty string", protocol.BAD_PARAMS)
    spoken.append(text)
    return {"accepted": True}


@pytest.fixture
def client(server):
    with ControlClient(server.path, timeout=5.0) as connected:
        yield connected


class TestRequests:
    def test_answers_a_command(self, client):
        assert client.request("ping") == {"pong": True}

    def test_a_command_reaches_its_handler(self, client, spoken):
        assert client.request("say", text="what time is it") == {"accepted": True}
        assert spoken == ["what time is it"]

    def test_unknown_commands_are_refused_by_name(self, client):
        with pytest.raises(ControlError) as caught:
            client.request("summon_demon")

        assert caught.value.code == protocol.UNKNOWN_COMMAND

    def test_bad_parameters_are_refused_with_a_reason(self, client):
        with pytest.raises(ControlError) as caught:
            client.request("say", text="")

        assert caught.value.code == protocol.BAD_PARAMS
        assert "non-empty" in str(caught.value)

    def test_a_failing_handler_does_not_drop_the_connection(self, client):
        with pytest.raises(ControlError) as caught:
            client.request("boom")
        assert caught.value.code == protocol.INTERNAL

        # Still usable afterwards, which is the point.
        assert client.request("ping") == {"pong": True}

    def test_several_requests_are_matched_to_their_own_answers(self, client, spoken):
        for index in range(10):
            client.request("say", text=f"line {index}")

        assert spoken == [f"line {index}" for index in range(10)]


class TestEvents:
    def test_no_events_arrive_before_subscribing(self, server):
        received: list[dict] = []
        with ControlClient(server.path, on_event=received.append) as client:
            client.request("ping")
            server.state_for_test.set_phase(THINKING)
            time.sleep(0.2)

        assert received == []

    def test_subscribing_delivers_the_current_state_immediately(self, server):
        received: list[dict] = []
        with ControlClient(server.path, on_event=received.append) as client:
            client.subscribe()
            time.sleep(0.2)

        assert received
        assert received[0]["event"] == "status"
        assert received[0]["data"]["pid"] == 4242

    def test_a_phase_change_is_pushed(self, server):
        received: list[dict] = []
        with ControlClient(server.path, on_event=received.append) as client:
            client.subscribe()
            time.sleep(0.1)
            server.state_for_test.set_phase(THINKING)
            time.sleep(0.2)

        phases = [frame["data"]["phase"] for frame in received]
        assert THINKING in phases


class TestSocketOwnership:
    def test_a_stale_socket_file_is_replaced(self, short_socket_path):
        """A crashed process leaves a file that looks exactly like a live one."""
        short_socket_path.parent.mkdir(parents=True, exist_ok=True)
        short_socket_path.write_text("not really a socket", encoding="utf-8")

        server = ControlServer(short_socket_path, {"ping": lambda params: "pong"})
        server.start()
        try:
            with ControlClient(short_socket_path) as client:
                assert client.request("ping") == "pong"
        finally:
            server.close()

    def test_a_live_socket_is_not_stolen(self, server):
        """Two assistants would fight over the microphone. Refuse the second."""
        second = ControlServer(server.path, {"ping": lambda params: "pong"})

        with pytest.raises(ControlServerBusy):
            second.start()

        # The original is untouched.
        with ControlClient(server.path) as client:
            assert client.request("ping") == {"pong": True}

    def test_closing_removes_the_socket_file(self, short_socket_path):
        server = ControlServer(short_socket_path, {})
        server.start()
        assert short_socket_path.exists()

        server.close()
        assert not short_socket_path.exists()


class TestClientWithoutAServer:
    def test_a_missing_socket_says_so(self, short_socket_path):
        with pytest.raises(NotRunning) as caught:
            ControlClient(short_socket_path).connect()

        assert "not running" in str(caught.value).lower()

    def test_a_stale_socket_says_so(self, short_socket_path):
        """Left behind by a crash: the file exists, nothing answers."""
        short_socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(short_socket_path))
        listener.close()  # bound, never listened, then gone

        with pytest.raises(NotRunning):
            ControlClient(short_socket_path).connect()


class TestNoticingTheServerGoAway:
    """A killed assistant cannot send a farewell, so EOF is the whole signal.

    Without this the dashboard's status bar stayed on its last known state
    forever: nothing set `connected = False`, so its reconnect timer saw a
    live connection every three seconds and never looked again.

    Each of these makes a round trip before killing the server. That is not
    ceremony: until a connection has actually been accepted it is still in the
    listener's backlog, and closing the listener leaves such a peer blocked in
    recv rather than waking it. The dashboard always calls `subscribe()`
    immediately after connecting, so its connection is established by the time
    any of this matters, and a server that dies in the gap is caught by that
    request timing out instead.
    """

    def wait_for(self, closed: list, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not closed and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_the_server_going_away_is_reported(self, server):
        closed: list[str] = []
        with ControlClient(server.path, timeout=5.0, on_close=closed.append) as client:
            client.request("ping")

            server.close()
            self.wait_for(closed)

        assert len(closed) == 1
        assert closed[0]

    def test_closing_it_ourselves_is_not_reported(self, server):
        """Every ordinary teardown would otherwise announce a disconnect."""
        closed: list[str] = []
        client = ControlClient(server.path, timeout=5.0, on_close=closed.append).connect()
        client.request("ping")

        client.close()
        time.sleep(0.2)

        assert closed == []

    def test_it_is_reported_once(self, server):
        """Closing after the peer already went is the dashboard's teardown."""
        closed: list[str] = []
        client = ControlClient(server.path, timeout=5.0, on_close=closed.append).connect()
        client.request("ping")

        server.close()
        self.wait_for(closed)
        client.close()
        time.sleep(0.2)

        assert len(closed) == 1

    def test_a_client_without_the_callback_still_survives_it(self, server):
        """on_close is optional; the reader thread must not raise without one."""
        client = ControlClient(server.path, timeout=5.0).connect()
        client.request("ping")

        server.close()
        time.sleep(0.2)

        with pytest.raises(NotRunning):
            client.request("ping")


class TestBackpressure:
    def test_a_peer_that_never_reads_cannot_stall_the_assistant(self, server):
        """The conversation thread publishes state; it must never wait on a socket."""
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.connect(str(server.path))
        raw.sendall(protocol.encode(protocol.request("1", "subscribe")))
        time.sleep(0.1)

        state = server.state_for_test
        finished = threading.Event()

        def publish() -> None:
            # Far more than the queue can hold, from a peer reading none of it.
            for index in range(WELL_PAST_THE_QUEUE):
                state.set_phase(THINKING if index % 2 else "listening")
            finished.set()

        threading.Thread(target=publish, daemon=True).start()

        assert finished.wait(10), "publishing blocked on a socket write"
        raw.close()


WELL_PAST_THE_QUEUE = 2000
