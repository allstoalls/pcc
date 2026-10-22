"""Function-local ``from M import f`` bindings in native, no-libpython code.

A function-local from-import of a native sibling export used to lower to a
static direct-call alias registered module-wide.  The alias ignored control
flow and outlived the function:

* ``if c: from .a import f`` / ``else: from .b import f`` followed by ``f()``
  after the join raised ``AttributeError`` (relative) or silently called
  ``b.f`` on the ``a`` path (absolute);
* the same shape inside a loop crashed the self-backend emit worker;
* a branch that binds the name by assignment left the import path's local
  slot NULL (``py_obj_call received NULL callable``);
* a function-local import overwrote the module's own ``f`` for functions
  emitted later.

Names with more than one provider or binding now get the executing runtime
binding, and overwritten module aliases are restored when the function ends.
"""
from __future__ import annotations

import os
import subprocess


_PROVIDER_A = "def assemble_file(text):\n    return 'a:' + text\n"
_PROVIDER_B = (
    "def assemble_file(text, phase_timing=None):\n"
    "    if phase_timing is not None:\n"
    "        return 'b-timed:' + text\n"
    "    return 'b:' + text\n"
)
_CONSUMER = '''\
def assemble_file(text):
    return "mod:" + text


def relative_after_join(text, target):
    if target == "a":
        from .a import assemble_file
    else:
        from .b import assemble_file
    return assemble_file(text)


def absolute_after_join(text, target):
    if target == "a":
        from pkg.a import assemble_file
    else:
        from pkg.b import assemble_file
    return assemble_file(text)


def loop_rebinding(text):
    out = []
    for target in ("a", "b"):
        if target == "a":
            from .a import assemble_file
        else:
            from .b import assemble_file
        out.append(assemble_file(text))
    return ",".join(out)


def import_or_assignment(text, target):
    if target == "a":
        from .b import assemble_file
    else:
        from .b import assemble_file as other
        assemble_file = other
    return assemble_file(text, phase_timing=1)


def local_import_first(text):
    from .a import assemble_file
    return assemble_file(text)


def module_function_after(text):
    return assemble_file(text)
'''
_MAIN = '''\
from pkg import mod


def main():
    print(mod.relative_after_join("x", "a"))
    print(mod.relative_after_join("x", "b"))
    print(mod.absolute_after_join("x", "a"))
    print(mod.absolute_after_join("x", "b"))
    print(mod.loop_rebinding("x"))
    print(mod.import_or_assignment("x", "a"))
    print(mod.import_or_assignment("x", "b"))
    print(mod.local_import_first("x"))
    print(mod.module_function_after("x"))


main()
'''
_EXPECTED = [
    "a:x",
    "b:x",
    "a:x",
    "b:x",
    "a:x,b:x",
    "b-timed:x",
    "b-timed:x",
    "a:x",
    "mod:x",
]


def test_function_local_from_imports_follow_control_flow_and_scope(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text(_PROVIDER_A, encoding="utf-8")
    (package / "b.py").write_text(_PROVIDER_B, encoding="utf-8")
    (package / "mod.py").write_text(_CONSUMER, encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text(_MAIN, encoding="utf-8")
    exe = tmp_path / "bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(main), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=60, cwd=tmp_path, env=env,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.splitlines() == _EXPECTED
