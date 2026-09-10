"""Owned optimization dispatch shared by host self compilation and pcc1.

``mem2reg`` routes to ``pcc.native_ir.mem2reg``, the full Cytron algorithm
over pcc's own IR model: dominance frontiers, phi insertion and a
dominator-tree renaming walk, with no llvmlite.  The textual subset in
``compiled_default_passes`` runs after it as a mop-up for the function shapes
the CFG builder refuses to read; both fail closed, so composing them cannot
produce IR that neither would have produced alone.
"""

from pcc.native_ir.mem2reg import mem2reg_text
from pcc.native_ir.instsimplify import simplify_module_text
from pcc.native_ir.simplifycfg import simplify_cfg_text
from pcc.native_ir.instcombine import instcombine_text
from pcc.native_ir.dce import dce_module_text
from pcc.native_ir.inline import inline_module

from .compiled_default_passes import (
    _has_py_cpy_call, _mem2reg_function, _sroa_function,
    run_compiled_default_tier,
)


OWNED_PASS_NAMES = ("mem2reg", "sroa", "instsimplify", "simplifycfg", "instcombine", "dce", "inline", "inline-defined")


def owns_passes(names: list[str]) -> bool:
    if not names:
        return False
    for name in names:
        if name not in OWNED_PASS_NAMES:
            return False
    return True


def _single_scalar_pass(text: str, name: str) -> str:
    out: list[str] = []
    body: list[str] = []
    in_function = False
    for line in text.splitlines(keepends=True):
        if not in_function and line.lstrip().startswith("define "):
            body = [line]
            in_function = True
        elif in_function:
            body.append(line)
            if line.strip() == "}":
                if name == "mem2reg":
                    out.extend(_mem2reg_function(body))
                else:
                    out.extend(_sroa_function(body))
                body = []
                in_function = False
        else:
            out.append(line)
    out.extend(body)
    return "".join(out)


def run_owned_passes(text: str, names: list[str], strict_no_libpython: bool) -> str:
    if not owns_passes(names):
        raise ValueError("unsupported owned optimizer pass list")
    if strict_no_libpython and _has_py_cpy_call(text):
        return text
    current = text
    index = 0
    while index < len(names):
        name = names[index]
        try:
            if name == "mem2reg" and index + 1 < len(names) and names[index + 1] == "sroa":
                current, _changed = mem2reg_text(current)
                current = run_compiled_default_tier(
                    current, ["mem2reg", "sroa"], strict_no_libpython=strict_no_libpython
                )
                index += 1
            elif name == "mem2reg":
                current, _changed = mem2reg_text(current)
                current = _single_scalar_pass(current, name)
            elif name == "sroa":
                current = _single_scalar_pass(current, name)
            elif name == "instsimplify":
                current, changed = simplify_module_text(current)
            elif name == "simplifycfg":
                current, changed = simplify_cfg_text(current)
            elif name == "instcombine":
                current, changed = instcombine_text(current)
            elif name == "dce":
                current, changed = dce_module_text(current)
            elif name == "inline":
                current, changed = inline_module(current)
            elif name == "inline-defined":
                current, changed = inline_module(current, include_definitions=True)
        except Exception as exc:
            _write_owned_pass_exc(name, index, exc)
            raise
        index += 1
    return current


def _write_owned_pass_exc(name: str, index: int, exc: BaseException) -> None:
    try:
        with open("/tmp/owned_passes_exc.txt", "w", encoding="utf-8") as stream:
            stream.write("pass=" + str(name) + "\n")
            stream.write("index=" + str(index) + "\n")
            stream.write("type=" + str(type(exc).__module__) + "." + str(type(exc).__name__) + "\n")
            stream.write("args=" + repr(exc.args) + "\n")
            stream.write("message=" + str(exc) + "\n")
    except Exception:
        pass
