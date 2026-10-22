from __future__ import annotations

from pcc.stdlib import copy
from pcc.stdlib import pickle


def test_pickle_roundtrip_primitives_and_containers():
    obj = {
        "none": None,
        "bool": True,
        "int": 7,
        "float": 1.5,
        "str": "simple",
        "bytes": b"abc",
        "list": [1, 2, 3],
        "tuple": ("x", "y"),
        "set": {1, 2},
    }
    payload = pickle.dumps(obj)
    assert isinstance(payload, bytes)
    out = pickle.loads(payload)
    assert out["none"] is None
    assert out["bool"] is True
    assert out["int"] == 7
    assert out["float"] == 1.5
    assert out["str"] == "simple"
    assert out["bytes"] == b"abc"
    assert out["list"] == [1, 2, 3]
    assert out["tuple"] == ("x", "y")
    assert out["set"] == {1, 2}


def test_pickle_dump_load_file_like():
    class File:
        def __init__(self):
            self.data = b""
        def write(self, data):
            self.data += data
        def read(self):
            return self.data

    f = File()
    pickle.dump({"x": 1}, f)
    assert pickle.load(f) == {"x": 1}


def test_copy_user_object_and_deepcopy_cycle():
    class Box:
        def __init__(self, value):
            self.value = value

    b = Box([1])
    shallow = copy.copy(b)
    assert shallow is not b
    assert shallow.value is b.value

    deep = copy.deepcopy(b)
    assert deep is not b
    assert deep.value == [1]
    assert deep.value is not b.value

    xs = []
    xs.append(xs)
    ys = copy.deepcopy(xs)
    assert ys is ys[0]
