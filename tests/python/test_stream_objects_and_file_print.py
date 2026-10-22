"""First-class ``sys.std*`` streams, dynamic file methods and ``print(file=)``.

In a ``--python-libpython=off`` build:

* ``sys.stderr`` as a value was the string ``"<sys.stderr>"``, so
  ``destination = sys.stderr; destination.write(...)`` raised, and with it
  every ``warnings.warn``;
* file objects had no dynamic attributes -- ``f.write`` on a file no static
  type described was an AttributeError;
* ``print(..., file=obj)`` for anything but a literal ``sys.stdout`` /
  ``sys.stderr`` needed libpython and failed the compile;
* calling a builtin exception class through a value (``category(message)``)
  built a plain instance with no message or ``args``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


SOURCE = """
import sys
import warnings


class Sink:
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        return len(text)

    def flush(self):
        self.parts.append("<flush>")


def make(category, message):
    return category(message)


out = sys.stdout
print(type(out).__name__, out.closed, out.writable(), out.readable())
print(out.write("via stored stdout\\n"))
out.writelines(["a\\n", "b\\n"])
print("x", "y", file=out)
print([s.fileno() for s in (sys.stdin, sys.stdout, sys.stderr)])

sink = Sink()
print(1, 2.5, "three", file=sink, flush=True)
print(*[4, 5], file=sink, sep=",")
print(file=sink)
print(sink.parts)
print("none file", file=None)
try:
    print("bad", sep=3, file=sink)
except TypeError as exc:
    print("TypeError:", exc)

with open("stream_tmp.txt", "w") as handle:
    writer = handle
    writer.write("hello\\nworld\\n")
    print(type(writer).__name__)
reader = open("stream_tmp.txt")
opened = reader
print(opened.readline(), end="")
print(opened.readlines(), opened.closed)
opened.close()
print(opened.closed)
with open("stream_tmp.txt", "rb") as raw:
    print(type(raw).__name__)

w = make(RuntimeWarning, "there")
print(repr(str(w)), w.args, type(w).__name__, isinstance(w, Warning))
c = UserWarning
print(repr(str(c("hi"))), c("hi").args)

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    warnings.warn("careful now")
    warnings.warn("explicit", RuntimeWarning)
print([(type(item.message).__name__, str(item.message)) for item in caught])
"""


def test_stream_objects_file_methods_and_file_print_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(SOURCE).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60, cwd=str(tmp_path)
    )
    assert native.returncode == 0, native.stderr
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )
    assert expected.returncode == 0, expected.stderr
    assert native.stdout == expected.stdout


def test_warnings_warn_writes_category_and_message_to_stderr(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "warn.py"
    exe = tmp_path / "warn.out"
    src.write_text(
        "import warnings\n"
        "warnings.warn('careful now')\n"
        "warnings.warn('explicit', RuntimeWarning)\n"
        "print('done')\n",
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert native.returncode == 0, native.stderr
    assert native.stdout == "done\n"
    assert "UserWarning: careful now" in native.stderr
    assert "RuntimeWarning: explicit" in native.stderr
