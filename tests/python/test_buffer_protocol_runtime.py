import pytest

from pcc.library.buffer_protocol_runtime import PyBUF_WRITABLE, request_buffer


def test_request_buffer_for_bytes():
    view = request_buffer(b"abc")
    assert view.nbytes == 3
    assert view.readonly is True


def test_request_writable_from_bytes_fails():
    with pytest.raises(BufferError):
        request_buffer(b"abc", flags=PyBUF_WRITABLE)


def test_request_writable_from_bytearray():
    assert request_buffer(bytearray(b"abc"), flags=PyBUF_WRITABLE).readonly is False
