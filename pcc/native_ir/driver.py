"""Standalone native optimizer entry: PASS_CSV INPUT.ll OUTPUT.ll.

This is an explicit, growing set of owned scalar/CFG passes, not a claim of
full LLVM O2 parity. The same entry is runnable by host Python or compiled pcc.
"""

import sys
import time
import os

from pcc.extern import c_int64, extern

from pcc.native_ir.instsimplify import simplify_module_text
from pcc.native_ir.simplifycfg import simplify_cfg_text
from pcc.native_ir.instcombine import instcombine_text
from pcc.native_ir.dce import dce_module_text
from pcc.native_ir.inline import inline_module


_heap_live_bytes = extern("pcc_os_heap_in_use_bytes", (), c_int64)
_heap_capacity_bytes = extern("pcc_os_heap_capacity_bytes", (), c_int64)


def optimize_ir(text: str, passes: str) -> str:
    for name in passes.split(","):
        started = time.perf_counter()
        if name == "instsimplify":
            text, changed = simplify_module_text(text)
        elif name == "simplifycfg":
            text, changed = simplify_cfg_text(text)
        elif name == "instcombine":
            text, changed = instcombine_text(text)
        elif name == "dce":
            text, changed = dce_module_text(text)
        elif name == "inline":
            text, changed = inline_module(text)
        elif name == "inline-defined":
            text, changed = inline_module(text, include_definitions=True)
        else:
            raise ValueError("unsupported owned IR pass: " + name)
        elapsed = time.perf_counter() - started
        sys.stderr.write(name + " changed=" + str(changed) + " seconds=" + str(elapsed) + "\n")
        if os.environ.get("PCC_OPT_PROFILE_MEMORY", "") == "1":
            live = _heap_live_bytes()
            capacity = _heap_capacity_bytes()
            sys.stderr.write(name + " heap_live=" + str(live) + " heap_capacity=" + str(capacity) + "\n")
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
