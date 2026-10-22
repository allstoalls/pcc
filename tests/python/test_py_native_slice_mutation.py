from __future__ import annotations

import subprocess
import textwrap
import os

from pcc.frontends.python.pipeline import compile_python


def test_list_slice_assignment_and_delete_stay_native(tmp_path):
    src = tmp_path / "slice_mutation.py"
    src.write_text(
        textwrap.dedent(
            """
            xs = [1, 2, 3, 4]
            xs[1:3] = [8, 9, 10]
            print(len(xs))
            print(xs[0])
            print(xs[1])
            print(xs[3])
            del xs[1:3]
            print(len(xs))
            print(xs[1])
            xs[2:1] = [11]
            print(len(xs))
            print(xs[2])
            print(xs[3])
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "slice_mutation.out"

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
    assert run.stdout == "5\n1\n8\n10\n3\n10\n4\n11\n4\n"


def test_list_extended_slice_assignment_and_delete_stay_native(tmp_path):
    src = tmp_path / "extended_slice_mutation.py"
    src.write_text(
        textwrap.dedent(
            """
            xs = [0, 1, 2, 3, 4, 5]
            xs[1:6:2] = [7, 8, 9]
            print(xs[1])
            print(xs[3])
            print(xs[5])
            del xs[::2]
            print(len(xs))
            print(xs[0])
            print(xs[1])
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "extended_slice_mutation.out"

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
    assert run.stdout == "7\n8\n9\n3\n7\n8\n"


def test_list_slice_delete_converts_mutating_index_bounds_once(tmp_path):
    src = tmp_path / "slice_delete_index_callback.py"
    src.write_text(
        textwrap.dedent(
            """
            class Bound:
                def __init__(self, owner, value):
                    self.owner = owner
                    self.value = value
                    self.calls = 0

                def __index__(self):
                    self.calls = self.calls + 1
                    self.owner.append(90 + self.value)
                    return self.value

            def main() -> None:
                xs = [0, 1, 2, 3]
                lo = Bound(xs, 1)
                hi = Bound(xs, 3)
                del xs[lo:hi]
                print(len(xs))
                print(xs[0], xs[1], xs[2], xs[3])
                print(lo.calls, hi.calls)
                ys = [0, 1, 2, 3, 4, 5, 6]
                del ys[1:7:2]
                print(len(ys))
                print(ys[0], ys[1], ys[2], ys[3])
                zs = [0, 1, 2, 3, 4, 5]
                del zs[5:0:-2]
                print(len(zs))
                print(zs[0], zs[1], zs[2])

            if __name__ == "__main__":
                main()
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "slice_delete_index_callback.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run_env = {**os.environ, "PCC_GC_BACKEND": "4"}
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == (
        "4\n0 3 91 93\n1 1\n"
        "4\n0 2 4 6\n"
        "3\n0 2 4\n"
    )


def test_list_set_slice_snapshots_self_and_converts_mutating_bounds_once(tmp_path):
    src = tmp_path / "slice_set_alias_callback.py"
    src.write_text(
        textwrap.dedent(
            """
            class Bound:
                def __init__(self, owner, value):
                    self.owner = owner
                    self.value = value
                    self.calls = 0

                def __index__(self):
                    self.calls = self.calls + 1
                    self.owner.append(90 + self.value)
                    return self.value

            def main() -> None:
                xs = [0, 1, 2, 3]
                xs[1:3] = xs
                print(len(xs))
                print(xs[0], xs[1], xs[2], xs[3], xs[4], xs[5])

                ys = [0, 1, 2, 3]
                lo = Bound(ys, 1)
                hi = Bound(ys, 3)
                ys[lo:hi] = [7, 8]
                print(len(ys))
                print(ys[0], ys[1], ys[2], ys[3], ys[4], ys[5])
                print(lo.calls, hi.calls)

                ts = [0, 1, 2, 3]
                ts[1:3] = (8, 9, 10)
                print(len(ts))
                print(ts[0], ts[1], ts[2], ts[3], ts[4])

                zs = [0, 1, 2, 3, 4, 5]
                zs[5:0:-2] = [9, 8, 7]
                print(len(zs))
                print(zs[0], zs[1], zs[2], zs[3], zs[4], zs[5])

            if __name__ == "__main__":
                main()
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "slice_set_alias_callback.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run_env = {**os.environ, "PCC_GC_BACKEND": "4"}
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=run_env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == (
        "6\n0 0 1 2 3 3\n"
        "6\n0 7 8 3 91 93\n"
        "1 1\n"
        "5\n0 8 9 10 3\n"
        "6\n0 7 2 8 4 9\n"
    )
