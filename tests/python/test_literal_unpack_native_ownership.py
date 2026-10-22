"""Execute literal-unpack owner transfer with callbacks under all GC backends."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent('''\
    import gc
    import weakref

    events = []
    references = []
    selected = LookupError("selected RHS failure")

    class Item:
        def __init__(self, value):
            self.value = value
        def __del__(self):
            events.append(self.value)
            gc.collect()

    def make(value):
        gc.collect()
        result = Item(value)
        references.append(weakref.ref(result))
        return result

    def fail():
        gc.collect()
        raise selected

    def fresh_error():
        try:
            fresh, unused = make("fresh-discard"), fail()
        except LookupError as error:
            assert error is selected
            try:
                fresh
            except UnboundLocalError:
                pass
            else:
                raise AssertionError("early target publication")
        else:
            raise AssertionError("RHS did not raise")

    def exercise():
        parts = [make("left"), make("right")]
        left, right = parts[0], parts[1]
        parts = None
        gc.collect()
        assert left.value == "left" and right.value == "right"
        old_left = left
        old_right = right
        left, right = right, left
        assert left is old_right and right is old_left
        left, left = right, left
        assert left is old_right and right is old_left
        try:
            left, right = make("discard"), fail()
        except LookupError as error:
            assert error is selected
        else:
            raise AssertionError("RHS did not raise")
        assert left is old_right and right is old_left
        fresh_error()
        # Replacing old owners invokes collecting finalizers. The later RHS
        # must stay rooted while the first target consumes its prior owner.
        old_left = None
        old_right = None
        left, right = make("replacement-left"), make("replacement-right")
        gc.collect()
        assert left.value == "replacement-left"
        assert right.value == "replacement-right"
        assert events.count("left") == 1 and events.count("right") == 1
        assert events.count("discard") == 1 and events.count("fresh-discard") == 1
        result, count = left, 1 << 70
        assert result is left and count == 1180591620717411303424

    def main():
        global selected
        exercise()
        selected = None
        gc.collect()
        gc.collect()
        assert len(events) == 6
        for reference in references:
            assert reference() is None
        print("literal-unpack-owners-ok")

    main()
''')


def test_literal_unpack_callback_program_reference(tmp_path):
    source = tmp_path / 'literal_unpack_reference.py'
    source.write_text(PROGRAM)
    ran = subprocess.run([sys.executable, str(source)], capture_output=True,
                         text=True, timeout=20)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == 'literal-unpack-owners-ok\n'


def test_literal_unpack_callback_program_owned_ir():
    from tests.python.test_slot_call_operand_roots import _emit

    text = _emit(PROGRAM)
    assert "unpack.literal.move" in text
    assert "@pcc_gc_root_move(" in text
    assert "@py_tls_exc_swap_slot(" in text
    assert "strict.nolib.stub" not in text


@pytest.mark.integration
def test_literal_unpack_callback_program_native_five_gc(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    source = tmp_path / 'literal_unpack_native.py'
    source.write_text(PROGRAM)
    binary = tmp_path / 'literal_unpack_native'
    python_program_compiler(str(source), str(binary), backend='self',
                            libpython_mode='off', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH='/nonexistent', PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == 'literal-unpack-owners-ok\n'
