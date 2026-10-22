"""Real host AArch64 emitter byte differential; no generated code execution."""
import ast
import pytest
from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_precise_stackmaps as stackmaps
from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import NativeObject, encode_native_object
from tests.managed_liveness_plane_reference import function

def _target_final_stackmap_ir(triple: str) -> str:
    return f'''
target triple = "{triple}"

@frame_map = internal constant i32 1

declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_thread_safepoint()
declare void @opaque_call(ptr)
declare ptr @py_continuation_new_typed(ptr, ptr, ptr)
declare i64 @py_err_occurred()
declare void @resume()

define i64 @probe(ptr %obj, i64 %limit) {{
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  call void @opaque_call(ptr %obj)
  br label %loop

loop:
  %i = phi i64 [ 0, %entry ], [ %next, %body ]
  call void @pcc_thread_safepoint()
  %cont = call ptr @py_continuation_new_typed(ptr @frame_map, ptr %root, ptr @resume)
  %err = call i64 @py_err_occurred()
  %failed = icmp ne i64 %err, 0
  br i1 %failed, label %failure, label %body

body:
  %next = add i64 %i, 1
  %again = icmp slt i64 %next, %limit
  br i1 %again, label %loop, label %done

failure:
  call void @pcc_gc_frame_leave(ptr %root)
  ret i64 -1

done:
  call void @pcc_gc_frame_leave(ptr %root)
  ret i64 %i
}}
'''.strip()

def _stale_managed_ssa_ir(triple: str, *, ambiguous: bool = False) -> str:
    selected = (
        "  %selected = select i1 %pick, ptr %derived, ptr %raw\n"
        if ambiguous
        else "  %selected = getelementptr i8, ptr %derived, i64 0\n"
    )
    return f'''
target triple = "{triple}"

@frame_map = internal constant i32 1

declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_thread_safepoint()
declare void @opaque_call(ptr)

define void @refresh(ptr %obj, ptr %raw, i1 %pick) {{
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  %before = load ptr, ptr %root, align 8
  %derived = getelementptr i8, ptr %before, i64 24
{selected.rstrip()}
  call void @pcc_thread_safepoint()
  call void @opaque_call(ptr %selected)
  call void @pcc_gc_frame_leave(ptr %root)
  ret void
}}
'''.strip()

@pytest.mark.usefixtures("precise_stackmap_tables")
@pytest.mark.parametrize("shape", ["loop", "derived", "ambiguous"])
def test_host_pco_equal(monkeypatch, shape):
    triple = "arm64-apple-darwin23.6.0"
    ir = (_target_final_stackmap_ir(triple) if shape == "loop" else
          _stale_managed_ssa_ir(triple, ambiguous=shape == "ambiguous"))
    candidate = stackmaps._native_managed_liveness
    namespace = dict(vars(stackmaps))
    node = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function()], type_ignores=[])
    exec(compile(ast.fix_missing_locations(node), "<pre-fold-production>", "exec"), namespace)
    outputs = []
    observed = []
    for implementation in (namespace["_native_managed_liveness"], candidate):
        def checked(*args):
            observed.append(len(args[2].block_names))
            return implementation(*args)
        monkeypatch.setattr(stackmaps, "_native_managed_liveness", checked)
        try:
            asm = emit_aarch64_darwin_asm(ir)
        except BackendUnavailable as error:
            assert shape == "ambiguous"
            outputs.append((type(error), str(error)))
            continue
        sections, undefined = assemble_file(asm)
        encoded = encode_native_object(NativeObject.from_sections(sections, undefined=undefined))
        outputs.append((asm, encoded))
    assert observed and len(observed) % 2 == 0
    assert outputs[0] == outputs[1]
    assert len(outputs[0][1]) > 0
    if shape == "ambiguous":
        assert outputs[0][0] is BackendUnavailable
        assert "ambiguous root provenance" in outputs[0][1]
    else:
        assert isinstance(outputs[0][1], bytes)
