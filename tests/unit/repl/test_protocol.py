"""Protocol contract tests for the P0 CodeAct REPL."""

from __future__ import annotations

import pytest

from qwenpaw.repl.protocol import (
    ProtocolError,
    decode_message,
    encode_message,
)


def test_protocol_roundtrip_preserves_unicode() -> None:
    message = {"id": "e1", "type": "exec", "code": "print('你好')"}
    assert decode_message(encode_message(message)) == message


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"id": "", "type": "exec"},
        {"id": "1", "type": "unknown"},
    ],
)
def test_protocol_rejects_invalid_envelope(message: dict) -> None:
    with pytest.raises(ProtocolError):
        encode_message(message)


def test_protocol_enforces_kernel_message_limit() -> None:
    message = {"id": "e1", "type": "log", "message": "x" * 100}
    with pytest.raises(ProtocolError, match="limit"):
        encode_message(message, max_bytes=32)
