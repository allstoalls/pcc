"""`obj.name(args, key=value)` loads the method instead of binding it.

With keywords the call used to go through `py_obj_getattr`, which binds a
method object -- a captures tuple and a copied signature -- for one call; it
now takes `py_obj_load_method` + `py_obj_call_method_kwargs` like positional
calls.  The receiver shapes below cover an instance, an overriding subclass
calling `super()` with keywords, builtin methods, a module function, a
staticmethod reached through an instance, `**` unpacking and a missing
attribute.
"""

import os
import subprocess
import sys


PROGRAM = '''import gc
import json


class Box:
    def __init__(self):
        self.items = []

    def put(self, value, *, scale=1, name="x"):
        self.items.append((name, value * scale))
        return len(self.items)


class Sub(Box):
    def put(self, value, *, scale=1, name="y"):
        return super().put(value, scale=scale * 10, name=name)


def kw(**kwargs):
    return sorted(kwargs.items())


class Holder:
    f = staticmethod(kw)


def dynamic(obj, v):
    return obj.put(v, scale=2, name="k")


def main():
    gc.collect()
    b = Box()
    print(dynamic(b, 3), b.items)
    s = Sub()
    print(dynamic(s, 4), s.items)
    d = {"a": 5}
    lst = [3, 1, 2]
    lst.sort(reverse=True)
    print(d.get("a", None), lst)
    print(json.dumps({"b": 1, "a": 2}, sort_keys=True))
    h = Holder()
    print(h.f(a=1, b=2))
    opts = {"scale": 3, "name": "z"}
    print(b.put(1, **opts), b.items)
    try:
        dynamic(object(), 1)
    except AttributeError:
        print("AttributeError")


main()
'''


def test_keyword_method_calls_match_cpython_on_every_gc(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "kw_method_calls.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "kw_method_calls"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"
