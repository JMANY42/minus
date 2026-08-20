"""The wire codec, on its own -- no sockets involved."""

from __future__ import annotations

import json

import pytest

from minus.control import protocol


class TestFraming:
    def test_a_frame_never_contains_a_literal_newline(self):
        """Newline framing only works because JSON escapes them. Prove it."""
        frame = protocol.encode(
            protocol.request("1", "say", {"text": "first line\nsecond line\r\nthird"})
        )

        assert frame.count(b"\n") == 1
        assert frame.endswith(b"\n")

    def test_round_trips_through_encode_and_decode(self):
        original = protocol.request("7", "say", {"text": "what time is it"})

        assert protocol.decode(protocol.encode(original).strip()) == original

    def test_non_ascii_survives(self):
        frame = protocol.encode(protocol.request("1", "say", {"text": "café — naïve"}))

        assert protocol.decode(frame)["params"]["text"] == "café — naïve"


class TestDecodeFailures:
    def test_rejects_a_different_protocol_version(self):
        line = json.dumps({"v": 99, "type": "request", "command": "say"})

        with pytest.raises(protocol.ProtocolError) as caught:
            protocol.decode(line)

        assert caught.value.code == protocol.UNSUPPORTED_VERSION

    def test_rejects_a_frame_with_no_version(self):
        with pytest.raises(protocol.ProtocolError) as caught:
            protocol.decode(json.dumps({"type": "request"}))

        assert caught.value.code == protocol.UNSUPPORTED_VERSION

    def test_rejects_malformed_json(self):
        with pytest.raises(protocol.ProtocolError) as caught:
            protocol.decode("{not json")

        assert caught.value.code == protocol.MALFORMED

    def test_rejects_json_that_is_not_an_object(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode("[1, 2, 3]")

    def test_rejects_an_oversized_frame(self):
        line = b'{"v":1,"padding":"' + b"x" * protocol.MAX_LINE_BYTES + b'"}'

        with pytest.raises(protocol.ProtocolError) as caught:
            protocol.decode(line)

        assert caught.value.code == protocol.OVERSIZED

    def test_rejects_invalid_utf8(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode(b'{"v":1,"x":"\xff\xfe"}')


class TestEnvelopes:
    def test_a_response_echoes_the_request_id(self):
        answer = protocol.response("7", {"accepted": True})

        assert answer["id"] == "7"
        assert answer["ok"] is True
        assert answer["result"] == {"accepted": True}

    def test_an_error_is_a_response_that_is_not_ok(self):
        answer = protocol.error("7", protocol.BAD_PARAMS, "text must be a string")

        assert answer["type"] == protocol.RESPONSE
        assert answer["ok"] is False
        assert answer["error"]["code"] == protocol.BAD_PARAMS

    def test_events_carry_no_id(self):
        frame = protocol.event("status", {"phase": "thinking"}, seq=3)

        assert "id" not in frame
        assert frame["seq"] == 3
        assert frame["data"]["phase"] == "thinking"

    def test_every_envelope_is_encodable(self):
        """A frame that cannot be serialized is a bug found at the worst moment."""
        for frame in (
            protocol.request("1", "say", {"text": "hi"}),
            protocol.response("1", {"ok": 1}),
            protocol.error("1", protocol.INTERNAL, "boom"),
            protocol.event("status", {"phase": "idle"}),
        ):
            assert protocol.decode(protocol.encode(frame)) == frame
