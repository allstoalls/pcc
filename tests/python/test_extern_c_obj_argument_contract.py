"""c_obj arguments are boxed objects, protected in source evaluation order."""
import re
import hashlib
import json
import os
import subprocess

from pcc.frontends.python.pipeline import compile_python


def _ir(tmp_path, source):
    path = tmp_path / "extern_objects.py"
    output = tmp_path / "extern_objects.ll"
    path.write_text(source)
    compile_python(str(path), str(output), emit_llvm_only=True, libpython_mode="off")
    return output.read_text()


def test_scalar_c_obj_arguments_never_become_integer_addresses(tmp_path):
    text = _ir(tmp_path, '''from pcc.extern import extern, c_obj, c_int64
inspect = extern("py_index_i64_checked", (c_obj,), c_int64)
def probe():
    return inspect(37)
''')
    body = re.search(r"define [^\n]*@user_[^\n]*probe\([^\n]*\).*?\{(.*?)\n\}", text, re.S)[1]
    assert "extern.addr.ptr" not in body
    assert "@pcc_gc_store_root(" in body
    assert "@pcc_gc_load_ptr(" in body
    assert "@py_index_i64_checked(" in body


def test_c_obj_argument_and_result_lifetimes_are_explicit(tmp_path):
    text = _ir(tmp_path, '''from pcc.extern import extern, c_obj, c_int64
get = extern("py_tuple_get", (c_obj, c_int64), c_obj)
def later():
    raise ValueError("later argument")
def probe():
    try:
        return get((object(),), later())
    except ValueError:
        return None
''')
    assert "extern.object.argument" in text
    assert "operand.tmp.rooted" in text
    assert "extern.object.result" in text
    assert "cpy.operand.pcc.cleanup" in text


def test_host_compile_executes_c_obj_contract_on_all_gc_backends(
    tmp_path, pcc_runtime_archive,
):
    """Host frontend compilation followed by native GC0–4 execution.

    This is deliberately separate from the integration test's pcc1 compiler
    gate. An explicit PCC_RUNTIME_ARCHIVE selects the prebuilt runtime.
    """
    from tests.integration.test_native_extern_c_obj import SOURCE

    source = tmp_path / "extern_objects.py"
    executable = tmp_path / ("extern_objects.exe" if os.name == "nt" else "extern_objects")
    source.write_text(SOURCE, encoding="utf-8")
    compile_python(
        str(source), str(executable), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    receipts = {
        "scope": "host compile_python -> native execution; not a native compiler gate",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_archive": str(pcc_runtime_archive),
        "runtime_sha256": hashlib.sha256(pcc_runtime_archive.read_bytes()).hexdigest(),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "runs": [],
    }
    receipt_path = tmp_path / "native-execution.json"
    for gc_backend in range(5):
        result = subprocess.run(
            [str(executable)],
            env=dict(os.environ, PCC_GC_BACKEND=str(gc_backend), PATH="/nonexistent"),
            capture_output=True, text=True, timeout=20,
        )
        receipts["runs"].append({
            "gc_backend": gc_backend, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        })
        receipt_path.write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == 0, (gc_backend, result.stdout, result.stderr)
        assert result.stdout == "extern-c-obj-ok\n", (gc_backend, result.stdout)
