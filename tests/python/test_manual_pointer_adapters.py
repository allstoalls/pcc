"""Implicit manual pointer ABIs cannot unpack managed argument tuples."""
import re
from pathlib import Path

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
from pcc.ir.optimization.driver import optimize_ir


def compile_ir(tmp_path, source, *, runtime_library=False):
    directory = tmp_path / 'py_runtime_probe' / 'py' if runtime_library else tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    src = directory / 'manual_adapter_probe.py'
    out = tmp_path / 'manual_adapter_probe.ll'
    src.write_text(source)
    compile_python(str(src), str(out), emit_llvm_only=True, python_library=True,
                   libpython_mode='off', backend='self',
                   target_triple='x86_64-unknown-linux-gnu')
    return out.read_text()


def body(text, name):
    match = re.search(r'^define[^\n]*@user_manual_adapter_probe_' + name + r'\([^\n]*\{\n(.*?)^}', text, re.M | re.S)
    assert match, name
    return match.group(1)


def test_manual_pointer_adapter_rejects_before_tuple_unwrap(tmp_path):
    text = compile_ir(tmp_path, '__pcc_runtime_port__ = True\ndef echo(value):\n    return value\n', runtime_library=True)
    adapter = body(text, 'echo_native_adapter')
    assert '@py_raise' in adapter
    assert '@py_tuple_get' not in adapter
    assert '@user_manual_adapter_probe_echo(' not in adapter


def test_freestanding_pointer_export_keeps_its_direct_implementation(tmp_path):
    text = compile_ir(tmp_path, '''from pcc.extern import c_abi_export
__pcc_freestanding__ = True
@c_abi_export("manual_adapter_direct_echo")
def echo(value):
    return value
''')
    assert re.search(r'define (?:external )?ptr @manual_adapter_direct_echo\(ptr %value\)', text)
    assert 'ret ptr %value' in text
    assert 'echo_native_adapter' not in text


def test_ordinary_managed_pointer_adapter_still_forwards(tmp_path):
    text = compile_ir(tmp_path, 'def echo(value):\n    return value\n')
    adapter = body(text, 'echo_native_adapter')
    assert '@py_tuple_get_known' in adapter
    assert '@user_manual_adapter_probe_echo(' in adapter


def test_runtime_scalar_only_adapter_still_forwards(tmp_path):
    text = compile_ir(tmp_path, '__pcc_runtime_port__ = True\ndef scalar(value: int) -> int:\n    return value + 1\n', runtime_library=True)
    adapter = body(text, 'scalar_native_adapter')
    assert '@py_tuple_get_known' in adapter
    assert '@user_manual_adapter_probe_scalar(' in adapter


def test_runtime_explicit_managed_pointer_adapter_still_forwards(tmp_path):
    text = compile_ir(tmp_path, '__pcc_runtime_port__ = True\ndef echo(value: str) -> str:\n    return value\n', runtime_library=True)
    adapter = body(text, 'echo_native_adapter')
    assert '@py_tuple_get_known' in adapter
    assert '@user_manual_adapter_probe_echo(' in adapter


def test_manual_frame_helper_reaches_owned_object_after_runtime_passes(tmp_path):
    source = '''from pcc.extern import extern, c_ptr, c_void, c_abi_export
from pcc.unsafe import define_global_i32, global_addr, stack_alloc, memset
__pcc_runtime_port__ = True
define_global_i32("manual_adapter_frame_map", 1)
enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
def register(slots) -> None:
    enter(global_addr("manual_adapter_frame_map"), slots)
    leave(slots)
@c_abi_export("manual_adapter_runtime_entry")
def runtime_entry() -> None:
    slots = stack_alloc(8)
    memset(slots, 0, 8)
    register(slots)
'''
    text = compile_ir(tmp_path, source, runtime_library=True)
    root = Path(__file__).resolve().parents[2]
    optimized = optimize_ir(text, runtime_ir_passes(str(root / 'pcc/runtime')))
    obj = emit_owned_object(optimized, 'x86_64-unknown-linux-gnu')
    assert obj.startswith(b'\x7fELF')
    adapter = body(optimized, 'register_native_adapter')
    assert '@py_tuple_get' not in adapter
    assert '@pcc_gc_frame_enter' not in adapter
