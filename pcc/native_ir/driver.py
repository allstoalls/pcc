"""Standalone native optimizer entry: PASS_CSV INPUT.ll OUTPUT.ll.

This is an explicit, growing set of owned scalar/CFG passes, not a claim of
full LLVM O2 parity. The same entry is runnable by host Python or compiled pcc.
"""

import sys
import time
import os

from pcc.extern import c_int64, extern

from pcc.py_frontend.compiled_owned_passes import owns_passes, run_owned_passes


_heap_live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
_heap_capacity_bytes = extern("pcc_os_heap_capacity_bytes", (), c_int64)


def optimize_ir(text: str, passes: str) -> str:
    names = passes.split(",")
    index = 0
    while index < len(names):
        name = names[index]
        selected = [name]
        # Production composes full CFG promotion with the bounded memory
        # cleanup for this pair. Keep that unit intact at the standalone entry.
        if name == "mem2reg" and index + 1 < len(names) and names[index + 1] == "sroa":
            selected.append("sroa")
            index += 1
        if not owns_passes(selected):
            raise ValueError("unsupported owned IR pass: " + name)
        started = time.perf_counter()
        previous = text
        text = run_owned_passes(text, selected, False)
        changed = text != previous
        name = ",".join(selected)
        elapsed = time.perf_counter() - started
        sys.stderr.write(name + " changed=" + str(changed) + " seconds=" + str(elapsed) + "\n")
        if os.environ.get("PCC_OPT_PROFILE_MEMORY", "") == "1":
            live = _heap_live_bytes()
            capacity = _heap_capacity_bytes()
            sys.stderr.write(name + " heap_live=" + str(live) + " heap_capacity=" + str(capacity) + "\n")
        index += 1
    return text


def main() -> None:
    if len(sys.argv) != 4:
        raise ValueError("expected PASS_CSV INPUT.ll OUTPUT.ll")
    with open(sys.argv[2], "r") as stream:
        text = stream.read()
    text = optimize_ir(text, sys.argv[1])
    with open(sys.argv[3], "w") as stream:
        stream.write(text)


if __name__ == "__main__":
    main()
