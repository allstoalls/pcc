from __future__ import annotations

import subprocess
import sys
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_returned_typed_nested_def_uses_native_closure(tmp_path):
    src = tmp_path / "closure.py"
    src.write_text(
        textwrap.dedent(
            """
            def make_adder(n: int):
                def add(x: int) -> int:
                    return x + n
                return add

            f = make_adder(3)
            print(f(4))
            """
        ),
        encoding="utf-8",
    )
    ll_path = tmp_path / "closure.ll"

    compile_python(
        str(src),
        str(ll_path),
        emit_llvm_only=True,
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    ir_text = ll_path.read_text(encoding="utf-8")
    assert "@py_func_new" in ir_text
    assert "@py_obj_call" in ir_text
    assert "call " not in "\n".join(
        line for line in ir_text.splitlines() if "py_cpy_wrap_pcc" in line
    )


def test_closure_heavy_runs_without_libpython(tmp_path):
    src = tmp_path / "closure_heavy.py"
    src.write_text(
        textwrap.dedent(
            """
            def make_adder(n: int):
                def add(x: int) -> int:
                    return x + n
                return add

            def main() -> None:
                adders = []
                i: int = 0
                while i < 1000:
                    adders.append(make_adder(i))
                    i = i + 1

                total: int = 0
                j: int = 0
                while j < 1000:
                    total = total + adders[j](j)
                    j = j + 1
                print(total)

            if __name__ == "__main__":
                main()
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "closure_heavy.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "999000\n"

    if sys.platform == "darwin":
        linked = subprocess.run(
            ["otool", "-L", str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert linked.returncode == 0
        assert "Python" not in linked.stdout
        assert "libpython" not in linked.stdout
    elif sys.platform.startswith("linux"):
        linked = subprocess.run(
            ["ldd", str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert linked.returncode == 0
        assert "libpython" not in linked.stdout + linked.stderr


def test_native_closure_sees_outer_rebind_after_value_capture(tmp_path):
    src = tmp_path / "closure_cell.py"
    src.write_text(
        textwrap.dedent(
            """
            def outer():
                x = 1
                def get():
                    return x
                f = get
                x = 2
                return f

            print(outer()())
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "closure_cell.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "2\n"


def test_native_closure_shares_outer_captured_dict_subscript_mutation(tmp_path):
    src = tmp_path / "closure_dict_cell.py"
    src.write_text(
        textwrap.dedent(
            """
            TARGET = "target"

            def invoke(callback, key: str) -> bool:
                return callback(key)

            def outer():
                labels = {}
                labels[TARGET] = 1

                def contains(key: str) -> bool:
                    return key in labels

                print(TARGET in labels)
                print(contains(TARGET))
                print(invoke(contains, TARGET))

            outer()
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "closure_dict_cell.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nTrue\nTrue\n"


def test_native_nonlocal_counter_stays_libpython_free(tmp_path):
    src = tmp_path / "nonlocal_counter.py"
    src.write_text(
        textwrap.dedent(
            """
            def outer():
                x = 1
                def inc():
                    nonlocal x
                    x = x + 1
                    return x
                return inc

            f = outer()
            print(f())
            print(f())
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "nonlocal_counter.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "2\n3\n"


def test_native_closure_captures_starred_unpack_target(tmp_path):
    src = tmp_path / "closure_starred_unpack.py"
    src.write_text(
        textwrap.dedent(
            """
            def outer():
                split = (7, 3, 5)
                mod, rem, *rest = split

                def inner():
                    offset = rest[0] if rest else 0
                    return (mod + rem + offset) % 10

                return inner()

            print(outer())
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "closure_starred_unpack.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "5\n"


_STARRED_TARGETS = """
def middle_star(values):
    first, *mid, last = values

    def show():
        return (first, mid, last)

    return show()


class Holder:
    pass


def attr_star():
    holder = Holder()
    head, *holder.tail = [1, 2, 3, 4]
    return head, holder.tail


print(middle_star([1, 2, 3, 4, 5]), middle_star("ab"), attr_star())
"""


def test_native_starred_unpack_into_captured_and_attribute_targets(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    """A captured or attribute ``*target`` stores like the other unpack targets."""
    import os

    src = tmp_path / "starred_targets.py"
    src.write_text(_STARRED_TARGETS, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(src)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    exe = tmp_path / "starred_targets.out"
    python_program_compiler(
        str(src), str(exe), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert run.returncode == 0, f"GC{gc}: {run.stderr}"
        assert run.stdout == expected.stdout, f"GC{gc}: {run.stdout}"
