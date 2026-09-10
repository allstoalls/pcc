from pathlib import Path
import subprocess
import sys

import pytest

from pcc.py_frontend import pipeline
from pcc.py_frontend.types import PyFrontendError


def test_pipeline_import_defers_runtime_abi_initialization():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pcc.py_frontend.pipeline; "
            "raise SystemExit(int('pcc.py_frontend.codegen.runtime_abi' in sys.modules))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _compile_freestanding(tmp_path: Path, source: str) -> str:
    src = tmp_path / "kernel.py"
    out = tmp_path / "kernel.ll"
    src.write_text(source, encoding="utf-8")
    pipeline.compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out.read_text(encoding="utf-8")


def test_freestanding_atomic_module_has_only_exported_intrinsic_body(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import atomic_load_i64, atomic_rmw_i64\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"kernel_add\")\n"
        "def kernel_add(slot) -> i64:\n"
        "    old: i64 = atomic_rmw_i64(\"add\", slot, 0, 1, \"acq_rel\")\n"
        "    return old + atomic_load_i64(slot, 0, \"acquire\")\n",
    )

    assert "define i64 @kernel_add(ptr %slot)" in ir_text
    assert "atomicrmw add" in ir_text
    assert "load atomic i64" in ir_text
    assert "define i32 @main" not in ir_text
    assert "define void @_pcc_py_module_top_" not in ir_text
    assert "define void @_pcc_py_module_fini_" not in ir_text


def test_freestanding_module_stays_runtime_independent_with_threads_enabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"identity\")\n"
        "def identity(value: i64) -> i64:\n"
        "    return value\n",
    )

    body = ir_text.split("define i64 @identity", 1)[1].split("}\n", 1)[0]
    assert "pcc_thread_stop_requested" not in body
    assert "pcc_thread_safepoint" not in body


def test_freestanding_module_docstring_is_compile_time_only(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        '"""raw kernel documentation"""\n'
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"identity\")\n"
        "def identity(value: i64) -> i64:\n"
        "    return value\n",
    )
    assert "define i64 @identity(i64 %value)" in ir_text
    assert "raw kernel documentation" not in ir_text


def test_freestanding_pointer_abi_fallthrough_uses_raw_null_not_py_none(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export, c_ptr\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"select_ptr\")\n"
        "def select_ptr(value, enabled: i64) -> c_ptr:\n"
        "    if enabled != 0:\n"
        "        return value\n"
        "    return value\n",
    )
    body = ir_text.split("define ptr @select_ptr", 1)[1].split("}\n", 1)[0]
    assert "@py_None" not in body
    assert "ret ptr %value" in body


def test_freestanding_void_unsafe_intrinsic_does_not_materialize_py_none(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import store_i8\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"write_byte\")\n"
        "def write_byte(dst, value: i64) -> None:\n"
        "    store_i8(dst, 0, value)\n",
    )

    body = ir_text.split("@write_byte", 1)[1].split("}\n", 1)[0]
    assert "store i8" in body
    assert "@py_None" not in body


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_freestanding_object_has_no_undefined_runtime_or_libc_symbols(
    tmp_path, emitter
):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import atomic_load_i64, atomic_rmw_i64\n"
        "__pcc_freestanding__=True\n"
        "@c_abi_export(\"helper\")\n"
        "def helper(slot) -> i64:\n"
        "    return atomic_load_i64(slot, 0, \"acquire\")\n"
        "@c_abi_export(\"entry\")\n"
        "def entry(slot) -> i64:\n"
        "    old: i64 = atomic_rmw_i64(\"add\", slot, 0, 1, \"acq_rel\")\n"
        "    return old + helper(slot)\n",
    )
    obj = tmp_path / ("kernel_" + emitter + ".o")
    if emitter == "llvm":
        source = tmp_path / "kernel.ll"
        source.write_text(ir_text, encoding="utf-8")
    else:
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "kernel.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")

    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert undefined.stdout.strip() == ""


def test_freestanding_directive_requires_no_libpython_library_mode(tmp_path):
    src = tmp_path / "kernel.py"
    out = tmp_path / "kernel.ll"
    src.write_text("__pcc_freestanding__ = True\n", encoding="utf-8")

    with pytest.raises(pipeline.PyPipelineError, match="python_library"):
        pipeline.compile_python(
            str(src),
            str(out),
            emit_llvm_only=True,
            libpython_mode="off",
        )

    with pytest.raises(pipeline.PyPipelineError, match="python-libpython=off"):
        pipeline.compile_python(
            str(src),
            str(out),
            emit_llvm_only=True,
            libpython_mode="auto",
            python_library=True,
        )


@pytest.mark.parametrize(
    "directive",
    [
        "__pcc_freestanding__ = False\n",
        "if True:\n    __pcc_freestanding__ = True\n",
        "__pcc_freestanding__: bool = True\n",
    ],
)
def test_freestanding_directive_fails_closed_on_noncanonical_scope_or_value(
    tmp_path, directive
):
    src = tmp_path / "kernel.py"
    out = tmp_path / "kernel.ll"
    src.write_text(directive, encoding="utf-8")
    with pytest.raises(
        pipeline.PyPipelineError,
        match="unconditional module-scope assignment",
    ):
        pipeline.compile_python(
            str(src),
            str(out),
            emit_llvm_only=True,
            libpython_mode="off",
            python_library=True,
        )

def test_freestanding_functions_require_c_abi_export(tmp_path):
    with pytest.raises(RuntimeError, match="require @c_abi_export: helper"):
        _compile_freestanding(
            tmp_path,
            "__pcc_freestanding__ = True\n"
            "def helper(value: i64) -> i64:\n"
            "    return value + 1\n",
        )


def test_freestanding_function_docstring_is_compile_time_only(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('documented')\n"
        "def documented(value: i64) -> i64:\n"
        "    '''function metadata only'''\n"
        "    return value + 1\n",
    )
    assert "define i64 @documented(i64 %value)" in ir_text
    assert "function metadata only" not in ir_text


def test_freestanding_literal_shift_has_no_managed_error_edge(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('high_byte')\n"
        "def high_byte(value: i64) -> i64:\n"
        "    return (value >> 8) & 255\n",
    )
    body = ir_text.split("define i64 @high_byte", 1)[1].split("}\n", 1)[0]
    assert "ashr i64 %value, 8" in body
    assert "py_exc_new" not in body


def test_freestanding_augmented_int_loop_stays_in_raw_i64_lane(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_typed_export\n"
        "from pcc.unsafe import load_i8, ptr_is_null\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_typed_export('copy_until_zero', 'i32', ('ptr', 'i64'))\n"
        "def copy_until_zero(message, capacity: i64) -> i64:\n"
        "    if ptr_is_null(message) or capacity <= 0:\n"
        "        return -1\n"
        "    index: i64 = 0\n"
        "    while index + 1 < capacity:\n"
        "        if load_i8(message, index) == 0:\n"
        "            return 0\n"
        "        index += 1\n"
        "    return -1\n",
    )
    body = ir_text.split("define i32 @copy_until_zero", 1)[1]
    body = body.split("}\n", 1)[0]
    assert "add i64" in body
    assert "pcc_gc_frame_enter" not in body
    assert "py_int_" not in body


def test_freestanding_explicit_i64_annotation_owns_machine_arithmetic(tmp_path):
    """A fixed-width lane is explicit in source, never inferred from ``int``."""
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('advance')\n"
        "def advance(value: i64) -> i64:\n"
        "    return value + 1\n",
    )
    body = ir_text.split("define i64 @advance", 1)[1].split("}\n", 1)[0]
    assert "add i64 %value, 1" in body
    assert "py_int_" not in body
    assert "pcc_gc_" not in body


def test_freestanding_explicit_u64_uses_unsigned_machine_operations(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc import u64\n"
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('scale')\n"
        "def scale(value: u64, limit: u64) -> u64:\n"
        "    if value < limit:\n"
        "        return 0\n"
        "    return (value // 3) >> 1\n",
    )
    body = ir_text.split("define i64 @scale", 1)[1].split("}\n", 1)[0]
    assert "icmp ult i64 %value, %limit" in body
    assert "udiv i64 %value, 3" in body
    assert "lshr i64" in body
    assert "sdiv i64" not in body
    assert "ashr i64" not in body
    assert "py_int_" not in body
    assert "pcc_gc_" not in body


def test_freestanding_python_int_arithmetic_fails_before_publication(tmp_path):
    with pytest.raises(
        RuntimeError,
        match=r"ordinary Python int.*pcc\.i64",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('advance')\n"
            "def advance(value: int) -> int:\n"
            "    return value + 1\n",
        )


@pytest.mark.parametrize("operator", ("-", "~"))
def test_freestanding_python_int_unary_fails_before_publication(
    tmp_path,
    operator,
):
    with pytest.raises(
        RuntimeError,
        match=r"ordinary Python int.*pcc\.i64",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('unary')\n"
            "def unary(value: int) -> int:\n"
            f"    return {operator}value\n",
        )


def test_freestanding_python_int_compare_fails_before_publication(tmp_path):
    with pytest.raises(
        RuntimeError,
        match=r"ordinary Python int.*pcc\.i64",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('less')\n"
            "def less(lhs: int, rhs: int) -> bool:\n"
            "    return lhs < rhs\n",
        )


def test_freestanding_python_int_out_of_i64_literal_fails_before_publication(
    tmp_path,
):
    with pytest.raises(
        PyFrontendError,
        match=r"ordinary Python int literal.*explicit pcc\.i64",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('huge')\n"
            "def huge() -> int:\n"
            f"    return {(1 << 70)}\n",
        )


@pytest.mark.parametrize(
    ("annotation", "literal"),
    (
        ("i64", str(1 << 63)),
        ("i64", str(-(1 << 63) - 1)),
        ("u64", "-1"),
        ("u64", str(1 << 64)),
    ),
)
def test_freestanding_raw_int_rejects_out_of_range_literal(
    tmp_path,
    annotation,
    literal,
):
    with pytest.raises(
        PyFrontendError,
        match=rf"does not fit pcc\.{annotation}",
    ):
        _compile_freestanding(
            tmp_path,
            f"from pcc import {annotation}\n"
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('literal')\n"
            f"def literal() -> {annotation}:\n"
            f"    return {literal}\n",
        )


def test_freestanding_u64_max_default_is_explicit_and_in_range(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc import u64\n"
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('identity')\n"
        f"def identity(value: u64 = {(1 << 64) - 1}) -> u64:\n"
        "    return value\n",
    )
    body = ir_text.split("define i64 @identity", 1)[1].split("}\n", 1)[0]
    assert "py_int_" not in body
    assert "pcc_gc_" not in body


def test_freestanding_raw_int_rejects_implicit_python_int_operand(tmp_path):
    with pytest.raises(
        PyFrontendError,
        match=r"does not implicitly convert ordinary Python int",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc import i64\n"
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('mixed')\n"
            "def mixed(raw: i64, semantic: int) -> i64:\n"
            "    return raw + semantic\n",
        )


def test_freestanding_raw_division_traps_without_managed_runtime(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('quotient')\n"
        "def quotient(lhs: i64, rhs: i64) -> i64:\n"
        "    return lhs // rhs\n",
    )
    body = ir_text.split("define i64 @quotient", 1)[1].split("}\n", 1)[0]
    assert "call void @llvm.trap()" in body
    assert "sdiv i64 %lhs, %rhs" in body
    assert "@py_exc_new" not in body
    assert "@py_int_" not in body
    assert "@pcc_gc_" not in body


def test_freestanding_cross_target_unsafe_lowering_uses_target_not_host(tmp_path):
    source = tmp_path / "linux_syscall.py"
    output = tmp_path / "linux_syscall.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import syscall6\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('getpid_raw')\n"
        "def getpid_raw() -> i64:\n"
        "    return syscall6(39, 0, 0, 0, 0, 0, 0)\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple="x86_64-unknown-linux-gnu",
    )
    ir_text = output.read_text(encoding="utf-8")
    assert 'target triple = "x86_64-unknown-linux-gnu"' in ir_text
    assert "call i64 asm sideeffect \"syscall\"" in ir_text


def test_freestanding_rejects_heap_extern_call(tmp_path):
    with pytest.raises(pipeline.PyPipelineError, match="outside its verified closure"):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export, c_int64, c_ptr, extern\n"
            "__pcc_freestanding__ = True\n"
            "malloc = extern(\"malloc\", (c_int64,), c_ptr)\n"
            "@c_abi_export(\"allocate\")\n"
            "def allocate(size: i64):\n"
            "    return malloc(size)\n",
        )


def test_freestanding_process_entry_extern_requires_exact_c_abi(tmp_path):
    with pytest.raises(pipeline.PyPipelineError, match="outside its verified closure"):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export, c_int, c_ptr, extern\n"
            "__pcc_freestanding__ = True\n"
            "c_main = extern('main', (c_ptr,), c_int)\n"
            "@c_abi_export('_start')\n"
            "def start(stack) -> i64:\n"
            "    return c_main(stack)\n",
        )


def test_freestanding_rejects_managed_runtime_and_allows_verified_local_calls(tmp_path):
    with pytest.raises(
        pipeline.PyPipelineError,
        match="managed-runtime reference|outside its verified closure",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export(\"managed\")\n"
            "def managed(value: i64) -> i64:\n"
            "    items = [value]\n"
            "    return items[0]\n",
        )

    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"helper\")\n"
        "def helper(value: i64) -> i64:\n"
        "    return value + 1\n"
        "@c_abi_export(\"entry\")\n"
        "def entry(value: i64) -> i64:\n"
        "    return helper(value)\n",
    )
    assert "define i64 @helper(i64 %value)" in ir_text
    assert "call i64 @helper(i64 %value)" in ir_text


def test_freestanding_verified_local_gc_abi_call_is_not_an_external_escape(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"pcc_gc_local_helper\")\n"
        "def helper(value: i64) -> i64:\n"
        "    return value + 1\n"
        "@c_abi_export(\"entry\")\n"
        "def entry(value: i64) -> i64:\n"
        "    return helper(value)\n",
    )
    assert "define i64 @pcc_gc_local_helper(i64 %value)" in ir_text
    assert "call i64 @pcc_gc_local_helper(i64 %value)" in ir_text


def test_freestanding_verified_local_gc_callback_address_is_not_an_external_escape(
    tmp_path,
):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern\n"
        "from pcc.unsafe import null\n"
        "__pcc_freestanding__ = True\n"
        "visit = extern('pcc_gc_visit_object_slots', (c_ptr, c_ptr, c_ptr), c_int64)\n"
        "@c_abi_export('pcc_gc_local_callback')\n"
        "def callback(slot, role: i64, context) -> None:\n"
        "    return\n"
        "@c_abi_export('pcc_gc_local_callback_probe')\n"
        "def probe(obj) -> i64:\n"
        "    return visit(obj, callback, null())\n",
    )
    assert "@pcc_gc_local_callback to ptr" in ir_text
    assert "call i64 @pcc_gc_visit_object_slots" in ir_text


def test_freestanding_allows_exact_readonly_gc_runtime_abi_import(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export, c_int64, extern\n"
        "__pcc_freestanding__ = True\n"
        "metric = extern(\n"
        "    'pcc_gc_backend4_fragmentation_score', (), c_int64\n"
        ")\n"
        "@c_abi_export('read_fragmentation')\n"
        "def read_fragmentation() -> i64:\n"
        "    return metric()\n",
    )
    body = ir_text.split("define i64 @read_fragmentation", 1)[1].split("}\n", 1)[0]
    assert "call i64 @pcc_gc_backend4_fragmentation_score()" in body


def test_freestanding_allows_only_registered_gc_cross_object_abi_imports(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import (\n"
        "    c_abi_export, c_int32, c_int64, c_ptr, c_void, extern,\n"
        ")\n"
        "__pcc_freestanding__ = True\n"
        "safepoint = extern('pcc_thread_safepoint', (), c_void)\n"
        "threads_enabled = extern('pcc_threads_enabled', (), c_int64)\n"
        "index_insert = extern('py_gc_index_insert', (c_ptr, c_ptr), c_int64)\n"
        "index_remove = extern('py_gc_index_remove', (c_ptr,), c_ptr)\n"
        "granule_live = extern(\n"
        "    'pcc_gc_granule_is_object_start', (c_ptr,), c_int64\n"
        ")\n"
        "granule_retire = extern(\n"
        "    'pcc_gc_granule_object_retire', (c_ptr,), c_int64\n"
        ")\n"
        "tripwire = extern(\n"
        "    'pcc_runtime_tripwire_fail',\n"
        "    (c_ptr, c_ptr, c_int32), c_void\n"
        ")\n"
        "@c_abi_export('tracking_probe')\n"
        "def tracking_probe(obj, node) -> i64:\n"
        "    if threads_enabled() != 0:\n"
        "        safepoint()\n"
        "        tripwire(obj, node, 0)\n"
        "        return -1\n"
        "    if granule_live(obj) == 1:\n"
        "        return granule_retire(obj)\n"
        "    result: i64 = index_insert(obj, node)\n"
        "    index_remove(obj)\n"
        "    return result\n",
    )
    body = ir_text.split("define i64 @tracking_probe", 1)[1].split("}\n", 1)[0]
    assert "call void @pcc_thread_safepoint()" in body
    assert "call i64 @pcc_threads_enabled()" in body
    assert "@pcc_runtime_tripwire_fail(ptr %obj" in body
    assert "call i64 @pcc_gc_granule_is_object_start(ptr %obj)" in body
    assert "call i64 @pcc_gc_granule_object_retire(ptr %obj)" in body
    assert "call i64 @py_gc_index_insert(ptr %obj, ptr %node)" in body
    assert "call ptr @py_gc_index_remove(ptr %obj)" in body


@pytest.mark.parametrize(
    "binding, call",
    [
        (
            "index_insert = extern('py_gc_index_insert', (c_ptr, c_ptr), c_ptr)\n",
            "    return index_insert(obj, node)\n",
        ),
        (
            "index_find = extern('py_gc_index_find', (c_ptr, c_ptr), c_ptr)\n",
            "    return index_find(obj, node)\n",
        ),
    ],
)
def test_freestanding_rejects_unregistered_gc_cross_object_abi_shape(
    tmp_path, binding, call
):
    with pytest.raises(
        pipeline.PyPipelineError,
        match="managed-runtime reference|outside its verified closure",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export, c_int64, c_ptr, extern\n"
            "__pcc_freestanding__ = True\n"
            + binding
            + "@c_abi_export('tracking_probe')\n"
            "def tracking_probe(obj, node):\n"
            + call,
        )


@pytest.mark.parametrize(
    "binding",
    [
        "metric = extern('pcc_gc_backend4_fragmentation_score', (), c_int32)\n",
        "metric = extern('pcc_gc_not_a_runtime_symbol', (), c_int64)\n",
        "metric = extern('py_list_new', (c_int64,), c_ptr)\n",
    ],
)
def test_freestanding_rejects_unverified_or_managed_runtime_abi_import(
    tmp_path, binding
):
    with pytest.raises(
        pipeline.PyPipelineError,
        match="managed-runtime reference|outside its verified closure",
    ):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import (\n"
            "    c_abi_export, c_int32, c_int64, c_ptr, extern,\n"
            ")\n"
            "__pcc_freestanding__ = True\n"
            + binding
            + "@c_abi_export('read_metric')\n"
            "def read_metric() -> i64:\n"
            "    return metric()\n",
        )


def test_freestanding_allows_only_registered_literal_gc_global_imports(tmp_path):
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import global_addr, load_i32\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('read_gc_debt')\n"
        "def read_gc_debt() -> i64:\n"
        "    return load_i32(global_addr('pcc_gc_debt_bytes'), 0)\n",
    )
    body = ir_text.split("define i64 @read_gc_debt", 1)[1].split("}\n", 1)[0]
    assert "@pcc_gc_debt_bytes" in body

    with pytest.raises(pipeline.PyPipelineError, match="managed-runtime reference"):
        _compile_freestanding(
            tmp_path,
            "from pcc.extern import c_abi_export\n"
            "from pcc.unsafe import global_addr, load_i32\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('read_fake')\n"
            "def read_fake() -> i64:\n"
            "    return load_i32(global_addr('pcc_gc_not_registered'), 0)\n",
        )


def test_freestanding_runtime_global_registry_is_a_static_pcc1_import():
    from pcc.py_frontend.pipeline_freestanding import (
        freestanding_gc_runtime_global_imports,
    )
    from pcc.py_frontend.codegen import layer1_support

    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert "is_freestanding_gc_runtime_global" in (
        exports["pcc.py_frontend.codegen.runtime_abi"]
    )
    assert freestanding_gc_runtime_global_imports(
        "global_addr('pcc_gc_debt_bytes')"
    ) == {"pcc_gc_debt_bytes"}


def test_freestanding_readonly_gc_registry_is_a_static_pcc1_import():
    from pcc.py_frontend.pipeline_freestanding import (
        freestanding_readonly_gc_runtime_imports,
    )
    from pcc.py_frontend.codegen import layer1_support

    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert "is_freestanding_gc_readonly_runtime_import" in (
        exports["pcc.py_frontend.codegen.runtime_abi"]
    )
    assert freestanding_readonly_gc_runtime_imports(
        "metric = extern('pcc_gc_relocation_set_size', (), c_int64)"
    ) == {"pcc_gc_relocation_set_size"}


def test_freestanding_cross_object_gc_registry_is_a_static_pcc1_import():
    from pcc.py_frontend.pipeline_freestanding import (
        freestanding_gc_cross_object_runtime_imports,
    )
    from pcc.py_frontend.codegen import layer1_support

    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert "is_freestanding_gc_cross_object_runtime_import" in (
        exports["pcc.py_frontend.codegen.runtime_abi"]
    )
    assert freestanding_gc_cross_object_runtime_imports(
        "metric = extern('pcc_gc_scheduler_root_count', (), c_int64)"
    ) == {"pcc_gc_scheduler_root_count"}


def test_freestanding_rejects_module_execution_and_exception_ir(tmp_path):
    # ``value = 1`` is no longer rejected: a module-scope integer literal is a
    # compile-time constant, folded into its uses before inference, so it needs
    # no module init to execute. See test_freestanding_module_constants.py,
    # which gates that on the renamed module emitting byte-identical IR.
    # Anything that would actually have to run at import time still fails here.
    with pytest.raises(RuntimeError, match="module-scope statements: Assign"):
        _compile_freestanding(
            tmp_path,
            "__pcc_freestanding__ = True\n"
            "value = 1 + 1\n",
        )

    with pytest.raises(
        pipeline.PyPipelineError,
        match="exception machinery",
    ):
        pipeline._validate_freestanding_ir(
            "define i64 @bad() {\nentry:\n  %x = landingpad { ptr, i32 }\n}\n"
        )


def test_freestanding_rejects_classes_and_non_scaffold_imports(tmp_path):
    with pytest.raises(RuntimeError, match="class definitions: Box"):
        _compile_freestanding(
            tmp_path,
            "__pcc_freestanding__ = True\n"
            "class Box:\n"
            "    pass\n",
        )
    with pytest.raises(RuntimeError, match="only support imports from"):
        _compile_freestanding(
            tmp_path,
            "from os import getpid\n"
            "__pcc_freestanding__ = True\n",
        )


def test_freestanding_accepts_generated_abi_constants_as_compile_time_scaffold(
    tmp_path,
):
    source = (
        Path(__file__).resolve().parents[2]
        / "pcc"
        / "py_runtime"
        / "py"
        / "freestanding_gc_sweep_slots.py"
    )
    out = tmp_path / "freestanding_gc_sweep_slots.ll"
    pipeline.compile_python(
        str(source),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = out.read_text(encoding="utf-8")
    assert "define i64 @pcc_gc_tracing_is_sweep_candidate" in ir_text
    managed_calls = [
        line
        for line in ir_text.splitlines()
        if " call " in line
        and ("@py_cpy_" in line or "@py_int_from_i64" in line)
    ]
    assert managed_calls == []


def test_freestanding_validator_rejects_direct_external_call():
    with pytest.raises(
        pipeline.PyPipelineError,
        match="outside its verified closure",
    ):
        pipeline._validate_freestanding_ir(
            "declare i64 @malloc(i64)\n"
            "define i64 @bad(i64 %n) {\n"
            "entry:\n"
            "  %p = call i64 @malloc(i64 %n)\n"
            "  ret i64 %p\n"
            "}\n"
        )
