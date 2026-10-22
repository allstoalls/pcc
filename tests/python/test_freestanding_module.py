from pathlib import Path
import platform
import re
import subprocess
import sys

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.types import PyFrontendError
from tests.owned_ir_validation import verify_ir_text


def test_pipeline_import_defers_runtime_abi_initialization():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pcc.frontends.python.pipeline; "
            "raise SystemExit(int('pcc.frontends.python.codegen.runtime_abi' in sys.modules))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _compile_freestanding(tmp_path: Path, source: str, target_triple=None) -> str:
    src = tmp_path / "kernel.py"
    out = tmp_path / "kernel.ll"
    src.write_text(source, encoding="utf-8")
    pipeline.compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple=target_triple,
    )
    return out.read_text(encoding="utf-8")


def _function(ir_text: str, name: str):
    module = verify_ir_text(ir_text)
    functions = [fn for fn in module.functions if fn.name == name]
    assert len(functions) == 1, name
    return functions[0]


def _function_body(ir_text: str, name: str) -> str:
    _function(ir_text, name)
    match = re.search(
        r"^define\b[^\n]*@" + re.escape(name) + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}",
        ir_text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, name
    return match.group(1)


def _assert_export(ir_text: str, name: str, return_type: str, parameters=()):
    from pcc.backend.self_backend_parse import parse_ir_type

    function = _function(ir_text, name)
    assert function.is_global
    assert function.ret_type == parse_ir_type(return_type)
    assert [(arg.type, arg.name) for arg in function.args] == [
        (parse_ir_type(ty), argument.removeprefix("%")) for ty, argument in parameters
    ]


def _assert_call(ir_text: str, owner: str, callee: str, return_type: str):
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_ir_type

    kernel = get_indexed_function_kernel(_function(ir_text, owner))
    calls = [
        kernel.diagnostic_call_data(call_id)
        for call_id in range(len(kernel.call_scalars) // 8)
        if kernel.call_texts[kernel.call_header(call_id).second] == callee
    ]
    assert len(calls) == 1, (owner, callee)
    assert calls[0][1] == parse_ir_type(return_type)
    return calls[0]


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

    _assert_export(ir_text, "kernel_add", "i64", [("ptr", "%slot")])
    assert "atomicrmw add" in ir_text
    assert "load atomic i64" in ir_text
    assert [fn.name for fn in verify_ir_text(ir_text).functions] == ["kernel_add"]


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

    body = _function_body(ir_text, 'identity')
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
    _assert_export(ir_text, "identity", "i64", [("i64", "%value")])
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
    body = _function_body(ir_text, 'select_ptr')
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

    body = _function_body(ir_text, "write_byte")
    assert "store i8" in body
    assert "@py_None" not in body


@pytest.mark.parametrize("target", [
    "arm64-apple-darwin", "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
])
def test_freestanding_object_has_no_undefined_runtime_or_libc_symbols(
    tmp_path, target
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
        target_triple=target,
    )
    from pcc.backend.owned_object_emit import emit_owned_object

    data = emit_owned_object(ir_text, target)
    if "darwin" in target:
        from pcc.backend.macho_spec import N_TYPE, N_UNDF, parse_object

        undefined = [s["name"] for s in parse_object(data).symbols()
                     if s["name"] and s["n_type"] & N_TYPE == N_UNDF]
    elif "windows" in target:
        from pcc.backend.coff_x86_64 import parse_object

        undefined = [s.name for s in parse_object(data).symbols if s.name and s.section == 0]
    else:
        from pcc.backend.elf_x86_64 import parse_relocatable

        undefined = [s.name for s in parse_relocatable(data).symbols if s.name and s.section_index == 0]
    assert undefined == []


@pytest.mark.pcc_gate(unavailable=None if sys.platform == "darwin" and platform.machine() == "arm64" else "requires Darwin arm64")
def test_freestanding_atomic_export_links_and_executes(tmp_path):
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.owned_object_emit import emit_owned_object

    triple = "arm64-apple-darwin"
    ir_text = _compile_freestanding(
        tmp_path,
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import atomic_load_i64, atomic_rmw_i64\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('kernel_add')\n"
        "def kernel_add(slot) -> i64:\n"
        "    old: i64 = atomic_rmw_i64('add', slot, 0, 1, 'acq_rel')\n"
        "    return old + atomic_load_i64(slot, 0, 'acquire')\n",
        target_triple=triple,
    )
    caller = '''declare i64 @kernel_add(ptr)
define i32 @main() {
entry:
  %slot = alloca i64, align 8
  store i64 3, ptr %slot, align 8
  %result = call i64 @kernel_add(ptr %slot)
  %exit = trunc i64 %result to i32
  ret i32 %exit
}
'''
    output = tmp_path / "atomic"
    output.write_bytes(link_executable([
        emit_owned_object(ir_text, triple), emit_owned_object(caller, triple),
    ], entry="_main"))
    output.chmod(0o755)
    assert subprocess.run([str(output)], timeout=10).returncode == 7


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
    _assert_export(ir_text, "documented", "i64", [("i64", "%value")])
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
    body = _function_body(ir_text, 'high_byte')
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
    body = _function_body(ir_text, 'copy_until_zero')
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
    body = _function_body(ir_text, 'advance')
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
    body = _function_body(ir_text, 'scale')
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
    body = _function_body(ir_text, 'identity')
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
    body = _function_body(ir_text, 'quotient')
    _assert_call(ir_text, "quotient", "llvm.trap", "void")
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
    _assert_export(ir_text, "helper", "i64", [("i64", "%value")])
    call = _assert_call(ir_text, "entry", "helper", "i64")
    assert call[4][0][1] == "value"


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
    _assert_export(ir_text, "pcc_gc_local_helper", "i64", [("i64", "%value")])
    call = _assert_call(ir_text, "entry", "pcc_gc_local_helper", "i64")
    assert call[4][0][1] == "value"


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
    call = _assert_call(ir_text, "pcc_gc_local_callback_probe", "pcc_gc_visit_object_slots", "i64")
    assert re.search(
        "%" + re.escape(call[4][1][1]) + r" = bitcast [^\n]*@pcc_gc_local_callback to ptr",
        _function_body(ir_text, "pcc_gc_local_callback_probe"),
    )


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
    body = _function_body(ir_text, 'read_fragmentation')
    _assert_call(ir_text, "read_fragmentation", "pcc_gc_backend4_fragmentation_score", "i64")


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
    body = _function_body(ir_text, 'tracking_probe')
    for callee, result in [
        ("pcc_thread_safepoint", "void"), ("pcc_threads_enabled", "i64"),
        ("pcc_runtime_tripwire_fail", "void"),
        ("pcc_gc_granule_is_object_start", "i64"),
        ("pcc_gc_granule_object_retire", "i64"),
        ("py_gc_index_insert", "i64"), ("py_gc_index_remove", "ptr"),
    ]:
        _assert_call(ir_text, "tracking_probe", callee, result)


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
        "from pcc.unsafe import global_addr, load_i64\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('read_gc_debt')\n"
        "def read_gc_debt() -> i64:\n"
        "    return load_i64(global_addr('pcc_gc_debt_bytes'), 0)\n",
    )
    body = _function_body(ir_text, 'read_gc_debt')
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
    from pcc.frontends.python.pipeline_freestanding import (
        freestanding_gc_runtime_global_imports,
    )
    from pcc.frontends.python.codegen import layer1_support

    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert "is_freestanding_gc_runtime_global" in (
        exports["pcc.frontends.python.codegen.runtime_abi"]
    )
    assert freestanding_gc_runtime_global_imports(
        "global_addr('pcc_gc_debt_bytes')"
    ) == {"pcc_gc_debt_bytes"}


def test_freestanding_readonly_gc_registry_is_a_static_pcc1_import():
    from pcc.frontends.python.pipeline_freestanding import (
        freestanding_readonly_gc_runtime_imports,
    )
    from pcc.frontends.python.codegen import layer1_support

    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert "is_freestanding_gc_readonly_runtime_import" in (
        exports["pcc.frontends.python.codegen.runtime_abi"]
    )
    assert freestanding_readonly_gc_runtime_imports(
        "metric = extern('pcc_gc_relocation_set_size', (), c_int64)"
    ) == {"pcc_gc_relocation_set_size"}


def test_freestanding_cross_object_gc_registry_is_a_static_pcc1_import():
    from pcc.frontends.python.pipeline_freestanding import (
        freestanding_gc_cross_object_runtime_imports,
    )
    from pcc.frontends.python.codegen import layer1_support

    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert "is_freestanding_gc_cross_object_runtime_import" in (
        exports["pcc.frontends.python.codegen.runtime_abi"]
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
        / "pcc" / "runtime"
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
    assert _function(ir_text, "pcc_gc_tracing_is_sweep_candidate").ret_type.bits == 64
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
