"""Comprehension targets outside a function body are GC roots.

A comprehension at module scope or inside a native lambda binds its target in
a fresh owned slot of the module-init (or lambda) function, but root
registration skipped scopes without a ``def`` (``current_func_def is None``).
Any call on the target then failed to compile ("slot-call name has no
authoritative registered root"), and under the tracing collectors the target
was not a root while the comprehension body ran.
"""

from __future__ import annotations

import os
import subprocess
import sys


_PROGRAM = '''
import gc


class Bag:
    def __init__(self):
        self.values = [1, 2, 3]

    def __iter__(self):
        return iter(self.values)


words = ["ab", "c\\u00e9"]
print([w.startswith("c") for w in words])
print([w.upper() for w in ["ab", "cd"]])
print([c.isascii() for c in "x\\u00e9\\u65e5"])
print([x + 1 for x in Bag()][2])
print({w: len(w.encode("utf-8")) for w in words})
print(sorted({w.strip() for w in [" a", "b "]}))
print([(gc.collect(), c)[1] for c in "\\u00e9\\U0001f680x"] == ["\\u00e9", "\\U0001f680", "x"])
print([[w.upper() for w in pair] for pair in [["a"], ["b", "c"]]])
lengths = lambda items: [item.upper() for item in items]
print(lengths(["p", "q"]))
'''


def test_module_scope_comprehension_targets_match_cpython(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "module_scope_comprehensions.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "module_scope_comprehensions"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
