"""An explicit gc.collect() on GC4 releases what the store buffer holds.

A GC4 store-buffer entry (an old owner's slot that received a young value)
owns a reference to the recorded value until the remembered-roots step
drains it. Only background steps drained the buffer; an explicit collect
did not, so a value stored after the last background step and then
overwritten stayed alive through every later collect and its __del__ never
ran. This program printed "128 159" on GC4 (the last ``anchor`` survived):
module-global assignment is such an old-to-young store, and the exact heap
state decides whether a background step drains it first.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''import gc
destroyed = 0
anchor = None
class Bomb:
    def __bool__(self):
        raise ValueError("truth")
class Items:
    def __init__(self, mode):
        self.mode = mode
        self.done = False
    def __iter__(self):
        return self
    def __next__(self):
        if self.mode == 1:
            raise ValueError("item")
        if self.done:
            raise StopIteration
        self.done = True
        if self.mode == 2:
            return Bomb()
        return True
class Source:
    def __init__(self, mode: int):
        self.mode = mode
    def __del__(self):
        global destroyed
        destroyed += 1
    def __iter__(self):
        global anchor
        if self.mode == 0:
            raise ValueError("iter")
        if self.mode == 4:
            return 5
        if self.mode == 3:
            anchor = None
            gc.collect()
        return Items(self.mode)
def make(mode) -> object:
    return Source(mode)
def consume(source):
    for item in source:
        if not item:
            return False
    return True
def main():
    global anchor
    caught = 0
    for repeat in range(32):
        for mode in range(3):
            try:
                consume(make(mode))
            except ValueError:
                caught += 1
        try:
            consume(make(4))
        except TypeError:
            caught += 1
        anchor = make(3)
        assert consume(anchor)
    gc.collect()
    print(caught, destroyed)
main()
'''


@pytest.mark.integration
def test_explicit_collect_releases_store_buffer_values_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "store_buffer_hold.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout == "128 160\n"
    binary = tmp_path / "store_buffer_hold"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
