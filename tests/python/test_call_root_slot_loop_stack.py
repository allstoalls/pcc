"""Regression: the call-return GC root slot must be an entry-block alloca.

A ``builder.alloca`` positioned at the call site re-executes on every loop
iteration; LLVM stack allocas are only reclaimed at function exit, so a hot
loop calling a user function overflowed the 8MB stack after ~500K
iterations (SIGSEGV in the py_incref prologue — guard-page hit). The fix
(`unary_call_lowering._call_user` -> `_alloca_in_entry`) reuses ONE slot
per call site; the per-iteration null store + store_root/unroot pair keeps
the rooting window balanced, which the __del__ canary count pins exactly.
"""

import os
import subprocess
import tempfile
import textwrap


def test_call_in_loop_reuses_entry_root_slot_and_stays_balanced():
    from pcc.frontends.python.pipeline import compile_python

    td = tempfile.mkdtemp(prefix="pcc_call_root_loop_")
    src = os.path.join(td, "prog.py")
    exe = os.path.join(td, "prog.out")
    with open(src, "w", encoding="utf-8") as f:
        f.write(
            textwrap.dedent(
                """
                counter = [0]

                class Obj:
                    def __del__(self):
                        counter[0] = counter[0] + 1

                def make() -> Obj:
                    return Obj()

                def main() -> int:
                    n: int = 1_000_000
                    i: int = 0
                    while i < n:
                        o = make()
                        o = None
                        i = i + 1
                    print(counter[0])
                    return 0

                main()
                """
            ).lstrip()
        )
    compile_python(src, exe, ir_scaffold_mode="on")
    result = subprocess.run([exe], capture_output=True, text=True, timeout=60)
    # Pre-fix: rc -11 (stack overflow from 1M per-iteration allocas).
    assert result.returncode == 0, result.stderr
    # Balance: the reused entry slot must not leak or double-release any
    # iteration's result — every Obj finalizes exactly once.
    assert result.stdout.strip() == "1000000"
