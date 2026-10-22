"""A forwarding wrapper is transparent to a deferred, parking call.

``pcc.virtual_thread.call(partial(parked, 42))`` ran ``parked``
synchronously inside the compiled ``functools.partial.__call__``: its first
park hit the synchronous boundary and the task lost its continuation
(``seen == [1, 2]``, result None; test_callback_sync_context's virtual_park
case). A def decorated with ``pcc.virtual_thread.transparent_call`` now
carries the transparent-call role, and its ``return forward_call(target,
args, kwargs)`` calls ``target`` in the context the wrapper itself was
called in; ``functools.partial.__call__`` is such a wrapper.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python


PROGRAM = r'''
import pcc.virtual_thread as vt
from functools import partial

seen = []


def parked(value, scale=1):
    seen.append(("in", value))
    vt.yield_now()
    seen.append(("out", value))
    return value * scale


def other():
    seen.append("other")


class Forwarder:
    def __init__(self, target):
        self.target = target

    @vt.transparent_call
    def __call__(self, *args, **kwargs):
        return vt.forward_call(self.target, args, kwargs)


def handler():
    first = vt.call(partial(parked, 2))
    second = vt.call(partial(parked, 3, scale=10))
    third = vt.call(Forwarder(parked), 4)
    nested = vt.call(partial(partial(parked, 5), scale=2))
    plain = vt.call(partial(max, 7), 1)
    return [first, second, third, nested, plain]


def main():
    task = vt.spawn(handler)
    vt.spawn(other)
    vt.run(1, 256)
    print(vt.result(task), vt.exception(task))
    print(seen)
    print(partial(pow, 2)(5), Forwarder(abs)(-8), partial(sorted, key=len)(["ccc", "a", "bb"]))


main()
'''

EXPECTED = (
    "[2, 30, 4, 10, 7] None\n"
    "[('in', 2), 'other', ('out', 2), ('in', 3), ('out', 3), ('in', 4), ('out', 4), "
    "('in', 5), ('out', 5)]\n"
    "32 8 ['a', 'bb', 'ccc']\n"
)


@pytest.mark.integration
def test_partial_and_forwarders_park_through_vthread_call_on_five_collectors(
    tmp_path, pcc_runtime_archive,
):
    source = tmp_path / "transparent_forwarding.py"
    source.write_text(PROGRAM, encoding="utf-8")
    executable = tmp_path / "transparent_forwarding"
    compile_python(str(source), str(executable), backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(executable)], capture_output=True, text=True,
                             timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout == EXPECTED, f"GC{backend}: " + ran.stdout


def test_forward_call_outside_the_returned_value_fails_closed(tmp_path):
    source = tmp_path / "invalid_forwarder.py"
    source.write_text(
        "import pcc.virtual_thread as vt\n"
        "class Forwarder:\n"
        "    def __init__(self, target):\n"
        "        self.target = target\n"
        "    @vt.transparent_call\n"
        "    def __call__(self, *args, **kwargs):\n"
        "        result = vt.forward_call(self.target, args, kwargs)\n"
        "        return result\n"
        "def handler():\n    return vt.call(Forwarder(abs), -1)\n"
        "task = vt.spawn(handler)\nvt.run(1, 32)\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="must be the returned value of a transparent_call function"):
        compile_python(str(source), str(tmp_path / "invalid.ll"), emit_llvm_only=True,
                       backend="self", libpython_mode="off", ir_scaffold_mode="on")


def test_parking_transparent_wrapper_fails_closed(tmp_path):
    source = tmp_path / "parking_forwarder.py"
    source.write_text(
        "import pcc.virtual_thread as vt\n"
        "@vt.transparent_call\n"
        "def forwarder(target, *args):\n"
        "    vt.yield_now()\n"
        "    return vt.forward_call(target, args, {})\n"
        "def handler():\n    return vt.call(forwarder, abs, -1)\n"
        "task = vt.spawn(handler)\nvt.run(1, 32)\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="transparent_call wrapper must not park"):
        compile_python(str(source), str(tmp_path / "parking.ll"), emit_llvm_only=True,
                       backend="self", libpython_mode="off", ir_scaffold_mode="on")


def test_forward_call_requires_a_transparent_function(tmp_path):
    source = tmp_path / "plain_forward.py"
    source.write_text(
        "import pcc.virtual_thread as vt\n"
        "def plain(target, *args):\n"
        "    return vt.forward_call(target, args, {})\n"
        "print(plain(abs, -3))\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="must be the returned value of a transparent_call function"):
        compile_python(str(source), str(tmp_path / "plain.ll"), emit_llvm_only=True,
                       backend="self", libpython_mode="off", ir_scaffold_mode="on")
