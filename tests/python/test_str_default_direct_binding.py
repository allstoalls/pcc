"""Omitted ``str`` defaults bind directly, as the very default object.

Python binds the object a ``def`` evaluated, so a call site may not rebuild a
``str`` default unless it rebuilds that object. Every omitted ``str`` default
used to send the whole call to the runtime binder: ``getattr`` of the method
by name, then a dynamic call, about 4.5x the IR of a direct call. The
compiler's own mixin methods (``_as_gc_ptr(value, name="")``) are called
across modules this way thousands of times.

A module pools one immortal object per ``str`` literal, so a same-module call
site names the default's own object. Across modules the defining module
publishes a pointer cell per pooled default value and the caller loads it.
Identity and values are checked natively on all five collectors.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

LIB = '''def tag(x, label="lib label", sep=" "):
    return label + sep + str(x)


def ident(x, label="lib label"):
    return label


class Box:
    def __init__(self, value, unit="u"):
        self.value = value
        self.unit = unit

    def show(self, prefix="box:", suffix=""):
        return prefix + str(self.value) + self.unit + suffix

    def get_prefix(self, prefix="box:"):
        return prefix
'''

MAIN = '''from pkg.lib import Box, ident, tag


def local(x, name="local name"):
    return name


def churn(n):
    out = []
    i = 0
    while i < n:
        out.append([str(i), i])
        i += 1
    return len(out)


def cross_ident():
    return ident(0)


def cross_method(box: Box):
    return box.get_prefix()


def main():
    print(tag(1), tag(2, "L"), tag(3, sep="-"))
    churn(300)
    print(cross_ident() is ident.__defaults__[0], cross_ident() is ident(1))
    box = Box(5)
    print(box.show(), box.show("p:"), box.show(suffix="!"), Box(6, "kg").show())
    print(cross_method(box) is Box.get_prefix.__defaults__[0])
    print(local(1) is local.__defaults__[0], local(2) is local(3))
    churn(300)
    print(tag(4), box.show())


main()
'''


def _write(root: Path) -> Path:
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lib.py").write_text(LIB, encoding="utf-8")
    (package / "main.py").write_text(MAIN, encoding="utf-8")
    return package / "main.py"


def _main_ir(tmp_path: Path) -> str:
    from pcc.frontends.python.pipeline import compile_python_multi

    entry = _write(tmp_path)
    output = tmp_path / "main.ll"
    compile_python_multi(
        [str(entry.parent / "lib.py"), str(entry)], str(output),
        module_names=["pkg.lib", "pkg.main"], entry_module="pkg.main",
        emit_llvm_only=True, backend="self", libpython_mode="off", ir_scaffold_mode="on",
        recursive_stdlib=False,
    )
    return output.read_text(encoding="utf-8")


def _body(text: str, name: str) -> str:
    match = re.search(r"^define [^\n]*@" + re.escape(name) + r"\([^\n]*\).*?^}", text, re.M | re.S)
    assert match, name
    return match.group(0)


def test_omitted_str_defaults_call_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    text = _main_ir(tmp_path)
    lowered = _body(text, "user_pkg_main_cross_method")
    assert re.search(r"call [^\n]*@user_pkg_lib_Box_get_prefix\(", lowered)
    assert "@py_obj_call_slots" not in lowered
    assert "@py_obj_load_method" not in lowered
    # The callee module's own default object, not a caller-side copy.
    assert re.search(r"load ptr, ptr @pcc_strdefault_pkg_lib_[0-9a-f]{32}\b", lowered)
    assert not re.search(r"@pcc_gc_(?:pin|unpin|release)\(ptr %str\.default\.", lowered)
    lowered = _body(text, "user_pkg_main_main")
    assert re.search(r"call [^\n]*@user_pkg_main_local\(", lowered)


def test_str_defaults_keep_identity_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    entry = _write(tmp_path)
    expected = subprocess.run([sys.executable, "-c", "import pkg.main"], cwd=tmp_path,
                              capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "main"
    compile_python(str(entry), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr[-2000:]
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
