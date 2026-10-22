"""Attribute chains through a known cross-module class read fields by index.

``self.parent.env`` where ``parent`` is annotated with another module's class
used to lower to a by-name ``py_obj_getattr``, which scans every field name
of the class. The receiver's inferred class now selects the indexed field
read, as a hinted local already did. Properties stay dynamic, a subclass
instance keeps the base layout, and a None receiver still raises.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

HOST = '''from pkg.helper import Helper, Reader


class Host:
    def __init__(self):
        self.env = {"a": 1, "b": 2}
        self.name = "host"
        self.helper = Helper(self)

    @property
    def size(self):
        return len(self.env) * 10


class SubHost(Host):
    def __init__(self):
        super().__init__()
        self.extra = [1, 2, 3]
        self.env = {"z": 26}
        self.name = "sub"


def main():
    host = Host()
    sub = SubHost()
    print(host.helper.read(), host.helper.label(), host.helper.size())
    print(sub.helper.read(), sub.helper.label(), sub.helper.size())
    reader = Reader(None)
    try:
        reader.read()
    except AttributeError:
        print("missing parent raises AttributeError")


main()
'''

HELPER = '''from __future__ import annotations


class Helper:
    def __init__(self, parent: Host):
        self.parent = parent

    def read(self) -> int:
        return len(self.parent.env)

    def label(self) -> str:
        return self.parent.name

    def size(self) -> int:
        return self.parent.size


class Reader:
    def __init__(self, parent: Host):
        self.parent = parent

    def read(self) -> int:
        return len(self.parent.env)
'''


def _write_package(root: Path) -> Path:
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "host.py").write_text(HOST, encoding="utf-8")
    (package / "helper.py").write_text(HELPER, encoding="utf-8")
    return package / "host.py"


def test_attr_receiver_with_known_class_reads_field_by_index():
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    exports = {"pkg.host": {"Host": {
        "kind": "class", "class_name": "Host", "field_names": ["env", "name", "helper"],
        "methods": [], "owning_module": "pkg.host",
        "field_types": [("env", ("dyn",)), ("name", ("dyn",)), ("helper", ("dyn",))],
    }}}
    typed = infer_module(parse_and_lift(HELPER, "helper.py", "pkg.helper"), external_exports=exports)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    codegen._skip_program_main = True
    codegen._strict_no_libpython = True
    merged = dict(codegen._native_module_exports or {})
    merged.update(exports)
    codegen._native_module_exports = merged
    text = str(codegen.generate(typed))
    read = re.search(r"define[^\n]*@user_pkg_helper_Helper_read\([^)]*\)[^{]*\{(.*?)\n\}", text, re.S).group(1)
    assert not re.search(r"py_obj_getattr[^\n]*pyattr\.env\b", read), read
    assert re.search(r"@py_instance_get_field\(ptr %self\.parent[^,]*, i32 0\)", read), read


def test_attr_receiver_fields_match_cpython_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    entry = _write_package(tmp_path)
    expected = subprocess.run(
        [sys.executable, "-c", "import pkg.host"], cwd=tmp_path,
        capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "host"
    compile_python(str(entry), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
