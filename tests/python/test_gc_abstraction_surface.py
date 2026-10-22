import os
from pathlib import Path
import subprocess
import textwrap

from pcc.frontends.python.codegen.runtime_abi import RUNTIME_SIGNATURES


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_HEADER = REPO_ROOT / "pcc" / "runtime" / "include" / "py_runtime.h"
PY_OBJ_PORT = REPO_ROOT / "pcc" / "runtime" / "py" / "py_obj.py"
LAYER1_CODEGEN = REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "layer1.py"


BACKEND_REFCOUNT = 0
BACKEND_TRICOLOR = 1
BACKEND_CONCURRENT = 2
BACKEND_GENERATIONAL = 3
BACKEND_COLORED_RELOCATING = 4
BACKEND_INVALID = 9

GC_SURFACE = [
    "pcc_gc_alloc",
    "pcc_gc_retain",
    "pcc_gc_release",
    "pcc_gc_load_ptr",
    "pcc_gc_store_ptr",
    "pcc_gc_store_root",
    "pcc_gc_frame_enter",
    "pcc_gc_frame_leave",
    "pcc_gc_safepoint",
    "pcc_gc_collect",
    "pcc_gc_pin",
    "pcc_gc_unpin",
    "pcc_gc_object_id",
    "pcc_gc_reset_relocation_set",
    "pcc_gc_select_relocation_set",
    "pcc_gc_relocation_set_contains",
    "pcc_gc_relocation_set_size",
    "pcc_gc_install_forwarding",
    "pcc_gc_relocate_copy",
    "pcc_gc_backend",
    "pcc_gc_set_backend",
    "pcc_gc_backend_name",
    "pcc_gc_telemetry",
    "pcc_gc_telemetry_reset",
    "pcc_gc_step",
]

GC_KIND_NAMES = [
    "PCC_GC_KIND_REFCOUNT_CYCLE",
    "PCC_GC_KIND_INCREMENTAL_TRICOLOR",
    "PCC_GC_KIND_CONCURRENT_MARK_SWEEP",
    "PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR",
    "PCC_GC_KIND_COLORED_RELOCATING",
]

GC_COUNTER_NAMES = [
    "PCC_GC_COUNTER_ALLOCATIONS",
    "PCC_GC_COUNTER_WRITE_BARRIERS",
    "PCC_GC_COUNTER_READ_BARRIERS",
    "PCC_GC_COUNTER_SAFEPOINTS",
    "PCC_GC_COUNTER_PIN_BALANCE",
    "PCC_GC_COUNTER_WORK_STEPS",
    "PCC_GC_COUNTER_DEBT_BYTES",
    "PCC_GC_COUNTER_MAX_PAUSE_US",
    "PCC_GC_COUNTER_MINOR_ALLOCATIONS",
    "PCC_GC_COUNTER_MINOR_COLLECTIONS",
    "PCC_GC_COUNTER_MINOR_BYTES",
    "PCC_GC_COUNTER_CMS_WORKER_STARTS",
    "PCC_GC_COUNTER_CMS_QUEUE_PUSHES",
    "PCC_GC_COUNTER_CMS_WORKER_DRAINS",
    "PCC_GC_COUNTER_CMS_MUTATOR_ASSISTS",
    "PCC_GC_COUNTER_RELOCATION_FORWARDS",
    "PCC_GC_COUNTER_RELOCATION_BARRIER_FORWARDS",
    "PCC_GC_COUNTER_RELOCATION_PIN_REJECTS",
    "PCC_GC_COUNTER_CMS_WORKER_TRACES",
    "PCC_GC_COUNTER_MINOR_ARENA_REFILLS",
    "PCC_GC_COUNTER_MINOR_ARENA_BUMPS",
    "PCC_GC_COUNTER_MINOR_ARENA_FALLBACKS",
    "PCC_GC_COUNTER_CMS_WORKER_STOPS",
    "PCC_GC_COUNTER_CMS_WB_FLUSHES",
]


def test_gc_abstraction_surface_is_public_runtime_abi():
    header = RUNTIME_HEADER.read_text(encoding="utf-8")
    for name in GC_SURFACE:
        assert name in header
        assert name in RUNTIME_SIGNATURES


def test_gc_backend_kinds_are_algorithmic_not_project_branded():
    header = RUNTIME_HEADER.read_text(encoding="utf-8")
    for name in GC_KIND_NAMES + GC_COUNTER_NAMES:
        assert name in header
    forbidden = [
        "PCC_GC_BACKEND_LUA",
        "PCC_GC_BACKEND_GO",
        "PCC_GC_BACKEND_OCAML",
        "PCC_GC_BACKEND_ZGC",
    ]
    for name in forbidden:
        assert name not in header


def test_pcc_python_refcount_backend_exports_gc_surface():
    py_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
    root_operations = (
        REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_gc_root_operations.py"
    ).read_text(encoding="utf-8")
    # pin/unpin (bodies unchanged) are owned by the strict root-operations
    # module, next to pcc_gc_take_pinned_slot; the rest stay in py_obj.
    root_owned = {"pcc_gc_pin", "pcc_gc_unpin"}
    for name in GC_SURFACE[:12]:
        owner, other = (
            (root_operations, py_obj) if name in root_owned else (py_obj, root_operations)
        )
        assert f'@c_abi_export("{name}")' in owner, name
        assert f'@c_abi_export("{name}")' not in other, name
    # The late GC surface moved out of py_gc_backend.py/py_gc_telemetry.py
    # into the freestanding GC modules as the collector policy migrated to
    # pcc-Python.  Each symbol must be exported by exactly the module that
    # owns it in the production archive.  The mapping below is derived from
    # the archive symbol table (nm): one owner per symbol.
    late_surface = {
        "pcc_gc_object_id": "freestanding_gc_forwarding_identity.py",
        "pcc_gc_reset_relocation_set": "py_gc_backend.py",
        "pcc_gc_select_relocation_set": "freestanding_gc_relocation_selector.py",
        "pcc_gc_relocation_set_contains": "py_gc_backend.py",
        "pcc_gc_relocation_set_size": "py_gc_backend.py",
        "pcc_gc_install_forwarding": "freestanding_gc_forwarding_identity.py",
        "pcc_gc_relocate_copy": "freestanding_gc_relocation_copy.py",
        "pcc_gc_backend": "py_gc_backend.py",
        "pcc_gc_set_backend": "py_gc_backend.py",
        "pcc_gc_backend_name": "py_gc_backend.py",
        "pcc_gc_telemetry": "py_gc_telemetry.py",
        "pcc_gc_telemetry_reset": "py_gc_backend.py",
        "pcc_gc_step": "freestanding_gc_barrier_dispatcher.py",
    }
    cache: dict[str, str] = {}
    for name, rel in late_surface.items():
        path = REPO_ROOT / "pcc" / "runtime" / "py" / rel
        if rel not in cache:
            cache[rel] = path.read_text(encoding="utf-8")
        assert f'@c_abi_export("{name}")' in cache[rel], (
            f"missing @c_abi_export for {name} in {rel}"
        )


def test_pcc_python_gc_backend_tracks_frame_stack_not_single_root_slot():
    py_gc_backend = (
        REPO_ROOT / "pcc" / "runtime" / "py" / "py_gc_backend.py"
    ).read_text(encoding="utf-8")
    mapped_roots = (
        REPO_ROOT
        / "pcc" / "runtime"
        / "py"
        / "freestanding_gc_mapped_roots.py"
    ).read_text(encoding="utf-8")
    object_root_seeding = (
        REPO_ROOT
        / "pcc" / "runtime"
        / "py"
        / "freestanding_gc_object_root_seeding.py"
    ).read_text(encoding="utf-8")
    mark_cycle = (
        REPO_ROOT
        / "pcc" / "runtime"
        / "py"
        / "freestanding_gc_common_mark_cycle.py"
    ).read_text(encoding="utf-8")
    seed_body = mark_cycle.split("def pcc_gc_seed_roots() -> None:", 1)[1]
    seed_body = seed_body.split("@c_abi_export", 1)[0]
    frame_roots_body = object_root_seeding.split(
        "def pcc_gc_gray_current_roots() -> None:", 1
    )[1]
    registered_body = mapped_roots.split(
        "def pcc_gc_visit_registered_root_slots", 1
    )[1].split('@c_abi_export("pcc_gc_gray_mapped_roots")', 1)[0]
    assert "pcc_gc_gray_current_roots()" in seed_body
    assert '_seed_roots = extern("pcc_gc_seed_roots"' in py_gc_backend
    # Root graying must walk the whole frame stack via the shared
    # mapped-root-slot visitor (slots ptr at frame+8, slot count at
    # frame+40), following the next link at frame+16 — not a single
    # root slot.
    assert "pcc_gc_visit_registered_root_slots(1, 1)" in frame_roots_body
    assert "pcc_gc_visit_mapped_root_slots(" in registered_body
    assert "load_ptr(frame, 8)" in registered_body
    assert "load_i64(frame, 40)" in registered_body
    assert "frame = load_ptr(frame, 16)" in registered_body


def test_pcc_python_collect_uses_gc_hook_not_direct_stub_return():
    py_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
    assert 'extern("py_gc_collect"' in py_obj
    collect_body = py_obj.split('@c_abi_export("pcc_gc_collect")', 1)[1]
    collect_body = collect_body.split('@c_abi_export("pcc_gc_immortalize")', 1)[0]
    assert "py_gc_collect()" in collect_body


def test_refcount_entrypoints_are_compatibility_not_codegen_surface():
    header = RUNTIME_HEADER.read_text(encoding="utf-8")
    gc_pos = header.index("/* ---- GC interface")
    refcount_pos = header.index("/* ---- INCREF/DECREF compatibility")
    assert gc_pos < refcount_pos


def test_python_codegen_uses_gc_surface_for_owned_ref_release():
    layer1 = LAYER1_CODEGEN.read_text(encoding="utf-8")
    assert 'runtime["py_incref"]' not in layer1
    assert 'runtime["py_decref"]' not in layer1


def test_gc_backend_selector_runs_in_no_libpython_binary(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_i32, malloc, null, ptr_add, store_i32, store_i64, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_obj)
        pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
        pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
        pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
        pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)
        pcc_gc_safepoint = extern("pcc_gc_safepoint", (), c_void)
        pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_REFCOUNT})
            print(pcc_gc_backend())
            print(pcc_gc_set_backend({BACKEND_TRICOLOR}))
            print(pcc_gc_backend())
            print(pcc_gc_set_backend({BACKEND_COLORED_RELOCATING}))
            print(pcc_gc_backend())
            print(pcc_gc_set_backend({BACKEND_INVALID}))
            print(pcc_gc_backend())
            # Startup work pending under the selected collector (for example
            # GC4 remembered roots under PCC_GC_BACKEND=4) is drained first,
            # so the step below is the idle one this sequence measures.
            while pcc_gc_step(1024) != 0:
                pass
            pcc_gc_telemetry_reset()
            print(pcc_gc_telemetry(5))
            print(pcc_gc_step(7))
            print(pcc_gc_telemetry(5))
            pcc_gc_safepoint()
            print(pcc_gc_telemetry(3))
            print(pcc_gc_telemetry(5))

            pcc_gc_set_backend({BACKEND_TRICOLOR})
            pcc_gc_telemetry_reset()
            o = pcc_gc_alloc(24, 2, 0)
            print(pcc_gc_telemetry(0))
            print(pcc_gc_telemetry(5))
            pcc_gc_release(o)

            owner = pcc_gc_alloc(24, 2, 0)
            child = pcc_gc_alloc(24, 2, 0)
            print(load_i32(child, 12) & 8)
            store_i32(owner, 12, 32)
            slot2 = malloc(8)
            store_ptr(slot2, 0, null())
            pcc_gc_store_ptr(owner, slot2, child)
            print(load_i32(child, 12) & 16)
            pcc_gc_step(1)
            print(load_i32(child, 12) & 32)
            print(pcc_gc_collect(0))
            free(slot2)

            slot = malloc(8)
            store_ptr(slot, 0, null())
            pcc_gc_set_backend({BACKEND_CONCURRENT})
            v = pcc_gc_alloc(24, 2, 0)
            pcc_gc_telemetry_reset()
            pcc_gc_store_ptr(null(), slot, v)
            print(pcc_gc_telemetry(1))
            pcc_gc_store_ptr(null(), slot, null())
            pcc_gc_release(v)

            owner3 = pcc_gc_alloc(24, 2, 0)
            child3 = pcc_gc_alloc(24, 2, 0)
            pcc_gc_store_ptr(owner3, slot, child3)
            print(load_i32(child3, 12) & 16)
            pcc_gc_step(1024)
            print(load_i32(child3, 12) & 32)
            pcc_gc_store_ptr(owner3, slot, null())
            pcc_gc_release(child3)
            pcc_gc_release(owner3)

            pcc_gc_set_backend({BACKEND_GENERATIONAL})
            gen = pcc_gc_alloc(24, 2, 0)
            print(load_i32(gen, 12) & 128)
            pcc_gc_release(gen)

            pcc_gc_set_backend({BACKEND_COLORED_RELOCATING})
            pcc_gc_telemetry_reset()
            pcc_gc_load_ptr(null(), slot)
            print(pcc_gc_telemetry(2))
            moving = pcc_gc_alloc(24, 2, 0)
            pcc_gc_pin(moving)
            print(load_i32(moving, 12) & 64)
            pcc_gc_unpin(moving)
            print(load_i32(moving, 12) & 64)
            pcc_gc_release(moving)
            free(slot)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "0", "0", "1", "0", "4", "-1", "4", "0", "0", "1", "1", "1",
            # No tracing step occurs between selecting backend #1 and this
            # store, so there is no active tricolor invariant: the white child
            # correctly remains white until the following explicit step.
            "1", "0", "8", "0", "32", "0", "1", "0", "32",
        "128", "0", "64", "0",
    ]


def test_tracing_gc_backend_preserves_owned_locals_and_frame_roots(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_i32, malloc, null, store_i32, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
        pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
        pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_TRICOLOR})
            rooted = pcc_gc_alloc(24, 2, 0)
            floating = pcc_gc_alloc(24, 2, 0)
            store_i32(floating, 12, load_i32(floating, 12) & ~16384)

            frame_map = malloc(4)
            root_slots = malloc(8)
            store_i32(frame_map, 0, 1)
            store_ptr(root_slots, 0, rooted)
            pcc_gc_frame_enter(frame_map, root_slots)

            # The return value is the number of work units processed, not the
            # number of objects allocated by this function. Module/runtime
            # roots may add work without changing the reachability contract.
            print(pcc_gc_step(1024) > 0)
            print(load_i32(rooted, 12) & 32)
            print(load_i32(rooted, 12) & 1024)
            print(load_i32(floating, 12) & 8)
            print(load_i32(floating, 12) & 1024)

            pcc_gc_frame_leave(root_slots)
            print(pcc_gc_step(1024) > 0)
            print(load_i32(rooted, 12) & 8)
            print(load_i32(rooted, 12) & 1024)
            print(load_i32(floating, 12) & 8)

            pcc_gc_release(floating)
            pcc_gc_release(rooted)
            free(root_slots)
            free(frame_map)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "True", "32", "0", "0", "0", "True", "0", "0", "0",
    ]


def test_tracing_gc_backend_traces_tuple_child_from_frame_root(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_i32, malloc, store_i32, store_i64, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)
        pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
        pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_TRICOLOR})
            tup = pcc_gc_alloc(32, 7, 0)
            child = pcc_gc_alloc(24, 2, 0)
            store_i64(tup, 16, 1)
            store_ptr(tup, 24, child)

            frame_map = malloc(4)
            root_slots = malloc(8)
            store_i32(frame_map, 0, 1)
            store_ptr(root_slots, 0, tup)
            pcc_gc_frame_enter(frame_map, root_slots)

            print(pcc_gc_collect(0))
            print(load_i32(tup, 12) & 32)
            print(load_i32(child, 12) & 32)

            pcc_gc_frame_leave(root_slots)
            pcc_gc_release(child)
            pcc_gc_release(tup)
            free(root_slots)
            free(frame_map)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["0", "32", "32"]


def test_tracing_gc_backend_traces_dict_and_instance_children(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_i32, malloc, null, store_i32, store_i64, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
        pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
        pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_TRICOLOR})
            frame_map = malloc(4)
            root_slots = malloc(8)
            store_i32(frame_map, 0, 1)

            d = pcc_gc_alloc(56, 6, 0)
            key = pcc_gc_alloc(24, 2, 0)
            value = pcc_gc_alloc(24, 2, 0)
            entries = malloc(24)
            store_i64(entries, 0, 1)
            store_ptr(entries, 8, key)
            store_ptr(entries, 16, value)
            store_ptr(d, 40, entries)
            store_i64(d, 48, 1)
            store_ptr(root_slots, 0, d)
            pcc_gc_frame_enter(frame_map, root_slots)
            pcc_gc_step(1024)
            pcc_gc_step(1024)
            print(load_i32(key, 12) & 32)
            print(load_i32(value, 12) & 32)
            pcc_gc_frame_leave(root_slots)

            cls = pcc_gc_alloc(96, 10, 0)
            inst = pcc_gc_alloc(40, 11, 0)
            child = pcc_gc_alloc(24, 2, 0)
            store_i32(cls, 72, 1)
            store_ptr(inst, 16, cls)
            store_ptr(inst, 24, child)
            store_ptr(inst, 32, null())
            store_ptr(root_slots, 0, inst)
            pcc_gc_frame_enter(frame_map, root_slots)
            pcc_gc_step(1024)
            pcc_gc_step(1024)
            print(load_i32(child, 12) & 32)
            pcc_gc_frame_leave(root_slots)
            free(entries)
            free(root_slots)
            free(frame_map)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["32", "32", "32"]


def test_tracing_gc_backend_traces_instance_class_child(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_i32, malloc, null, store_i32, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
        pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
        pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_TRICOLOR})
            frame_map = malloc(4)
            root_slots = malloc(8)
            store_i32(frame_map, 0, 1)

            cls = pcc_gc_alloc(104, 10, 0)
            inst = pcc_gc_alloc(32, 11, 0)
            store_i32(cls, 24, 0)      # n_bases
            store_ptr(cls, 32, null()) # bases
            store_i32(cls, 40, 0)      # n_mro
            store_ptr(cls, 48, null()) # mro
            store_i32(cls, 56, 0)      # n_methods
            store_ptr(cls, 64, null()) # methods
            store_i32(cls, 72, 0)      # n_fields
            store_i32(cls, 88, 32)     # instance_size
            store_i32(cls, 92, 11)     # type_tag_alloc
            store_ptr(inst, 16, cls)
            store_ptr(inst, 24, null())
            store_ptr(root_slots, 0, inst)
            pcc_gc_frame_enter(frame_map, root_slots)

            i = 0
            while i < 8:
                if pcc_gc_step(1024) == 0:
                    break
                i = i + 1
            print(load_i32(inst, 12) & 32)
            print(load_i32(cls, 12) & 32)
            print(load_i32(cls, 12) & 1024)

            pcc_gc_frame_leave(root_slots)
            free(root_slots)
            free(frame_map)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["32", "32", "0"]


def test_tracing_gc_backend_traces_coroutine_children(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, load_i32, malloc, store_i32, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)
        pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
        pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_TRICOLOR})
            frame_map = malloc(4)
            root_slots = malloc(8)
            store_i32(frame_map, 0, 1)

            coro = pcc_gc_alloc(64, 20, 0)
            captures = pcc_gc_alloc(24, 2, 0)
            args = pcc_gc_alloc(24, 2, 0)
            result = pcc_gc_alloc(24, 2, 0)
            store_ptr(coro, 32, captures)
            store_ptr(coro, 40, args)
            store_ptr(coro, 48, result)
            store_ptr(root_slots, 0, coro)
            pcc_gc_frame_enter(frame_map, root_slots)
            print(pcc_gc_collect(0))
            print(load_i32(captures, 12) & 32)
            print(load_i32(args, 12) & 32)
            print(load_i32(result, 12) & 32)
            pcc_gc_frame_leave(root_slots)
            free(root_slots)
            free(frame_map)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["0", "32", "32", "32"]


def test_generational_gc_surface_reports_young_allocations(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import load_i32

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)

        def main() -> None:
            pcc_gc_telemetry_reset()
            obj = pcc_gc_alloc(64, 2, 0)
            print(load_i32(obj, 12) & 128)
            print(pcc_gc_step(2) >= 0)
            pcc_gc_release(obj)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    env = {**os.environ, "PCC_GC_BACKEND": str(BACKEND_GENERATIONAL)}
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "128", "True",
    ]


def test_colored_relocating_gc_read_barrier_clears_candidate(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(f"""
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj, c_rawptr
        from pcc.unsafe import free, load_i32, malloc, null, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_set_backend = extern("pcc_gc_set_backend", (c_int64,), c_int64)
        pcc_gc_select_relocation_set = extern("pcc_gc_select_relocation_set", (c_int64,), c_int64)
        # The read barrier returns a borrowed pointer; c_obj would release it.
        pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_rawptr)
        pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
        py_list_new = extern("py_list_new", (c_int64,), c_obj)

        def main() -> None:
            pcc_gc_set_backend({BACKEND_COLORED_RELOCATING})
            # obj is an owned c_obj local: the compiler releases it at exit.
            obj = py_list_new(0)
            slot = malloc(8)
            store_ptr(slot, 0, null())
            pcc_gc_store_ptr(null(), slot, obj)
            print(pcc_gc_select_relocation_set(1))
            print(load_i32(obj, 12) & 2048)
            pcc_gc_load_ptr(null(), slot)
            print(load_i32(obj, 12) & 2048)
            pcc_gc_store_ptr(null(), slot, null())
            free(slot)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1", "2048", "0"]
