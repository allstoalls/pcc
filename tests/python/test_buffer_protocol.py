from pcc.library.buffer_protocol import BufferView, PyBUF_ND, PyBUF_WRITABLE


def test_readonly_rejects_writable_flag():
    try:
        BufferView(object(), 1).check_flags(PyBUF_WRITABLE)
    except BufferError:
        pass
    else:
        raise AssertionError("expected BufferError")


def test_nd_requires_shape():
    try:
        BufferView(object(), 1).check_flags(PyBUF_ND)
    except BufferError:
        pass
    else:
        raise AssertionError("expected BufferError")
