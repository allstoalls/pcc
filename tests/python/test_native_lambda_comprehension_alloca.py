"""Regression: comprehension inside a native lambda allocas in the lambda.

Root cause (pproxy ``pcc1 -m pproxy`` self-backend emission failure:
``self backend expected pointer value 'st.addr.N' in
'user_pproxy_verbose__native_lambda_2'``): the native-lambda emitters swap
``builder``/``env``/``current_function`` but did not swap
``_current_entry_block``, which ``_alloca_in_entry`` targets. A comprehension
inside the lambda body then alloca'd its target slot into the ENCLOSING
function's entry block, producing a cross-function alloca reference that the
self backend rejects at materialization.

The snippet mirrors ``projects/python-proxy/pproxy/verbose.py``'s
``modstat`` shape. Pre-fix the compile below fails; post-fix it succeeds and
the comprehension target's alloca lives inside the lambda function itself.
"""

from __future__ import annotations

import os
import re
import subprocess

from pathlib import Path


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found walking up")


REPO = _repo_root()

_SNIPPET = '''
def modstat(tostat: tuple):
    return lambda i: lambda s: [st.__setitem__(i, st[i] + s) for st in tostat]


def main() -> int:
    a = [0, 0, 0]
    b = [0, 0, 0]
    f = modstat((a, b))
    print(len((a, b)))
    return 0
'''


def test_lambda_comprehension_target_allocas_in_lambda(tmp_path) -> None:
    src = tmp_path / "lambda_comp_probe.py"
    src.write_text(_SNIPPET)
    dump_dir = tmp_path / "irdump"
    dump_dir.mkdir()
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_DEBUG_SELF_IR_DUMP_DIR"] = str(dump_dir)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(tmp_path / "lambda_comp_probe_bin"),
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=240,
    )
    # Pre-fix this failed with "self backend expected pointer value
    # 'st.addr.N'" because the comprehension target alloca landed in the
    # enclosing function.
    assert proc.returncode == 0, proc.stderr + proc.stdout

    lls = sorted(dump_dir.glob("*.ll"))
    assert lls, "no IR dumped"
    # The target slot is ``st.addr`` in the old comprehension path and
    # ``st.for.obj.addr`` since comprehensions bind through the owned loop
    # target helper; either way it must belong to the lambda function.
    slot = re.compile(r"st\.(?:for\.obj\.)?addr")
    text = None
    for ll in lls:
        t = ll.read_text()
        if slot.search(t):
            text = t
            break
    assert text is not None, "comprehension target alloca not found in dump"

    cur_fn = None
    alloca_fn = None
    use_fns: set[str] = set()
    for ln in text.splitlines():
        if ln.startswith("define"):
            cur_fn = ln.split("@", 1)[1].split("(")[0]
        if cur_fn is None:
            continue
        if re.search(r"%st\.(?:for\.obj\.)?addr[\w.]* = alloca", ln):
            alloca_fn = cur_fn
        elif slot.search(ln):
            use_fns.add(cur_fn)

    assert alloca_fn is not None, "st.addr alloca missing"
    assert "lambda" in alloca_fn, (
        f"comprehension target alloca must live in the lambda function, "
        f"found in {alloca_fn!r}"
    )
    assert use_fns <= {alloca_fn}, (
        f"st.addr referenced outside its defining function: alloca in "
        f"{alloca_fn!r}, uses in {sorted(use_fns)}"
    )
