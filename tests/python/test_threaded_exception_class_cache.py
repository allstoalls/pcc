"""Cold builtin exception lookup publishes one complete class identity."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import re

import pytest

from pcc.runtime.py.py_abi_constants import (
    PYCLASSOBJECT_MRO_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_IMMORTAL,
    PY_TYPE_CLASS,
    PY_TYPE_EXC,
)


PORT_DIR = Path(__file__).resolve().parents[2] / "pcc/runtime/py"
PORT = PORT_DIR / "py_exc_table.py"


class _Class:
    def __init__(self, name, base):
        self.name = name
        self.flags = PY_FLAG_IMMORTAL
        self.mro = [self] + ([] if base is None else base.mro)
        self.alive = True


class _Exception:
    def __init__(self, cls):
        self.cls = cls


class _ColdMemory:
    """Host intrinsic oracle with a forced collision in each cold constructor.

    Execute the actual port functions. Only raw memory, class construction,
    atomics and root operations are replaced; matching executes its real MRO
    walk. Barriers keep every cache entry cold until all callers build it.
    """

    def __init__(self, workers, leaf):
        self.lock = threading.RLock()
        self.cache = [None] * 65
        names = ["unused"] * 65
        parents = [1] * 65
        names[0], names[1] = "BaseException", "Exception"
        names[leaf] = "StopIteration" if leaf == 8 else "RuntimeError"
        parents[0], parents[1] = -1, 0
        self.globals = {
            "py_exc_classes": self.cache,
            "PY_EXC_BUILTIN_NAMES": names,
            "PY_EXC_PARENT": parents,
            "pcc_exc_cache_candidate_frame_map": 1,
        }
        self.barriers = {
            name: threading.Barrier(workers) for name in (names[0], names[1], names[leaf])
        }
        self.objects = {}
        self.created = []
        self.retired = []
        self.roots = {}
        self.allocations = {}

    def class_new(self, name, bases, n_bases, _fields, _n_fields):
        base = bases[0] if n_bases else None
        cls = _Class(name, base)
        with self.lock:
            self.objects[id(cls)] = cls
            self.created.append(cls)
        self.barriers[name].wait(timeout=5)
        return cls

    def load_ptr(self, value, offset):
        if isinstance(value, _Class):
            assert value.alive
            assert offset == PYCLASSOBJECT_MRO_OFFSET
            return value.mro
        if isinstance(value, _Exception):
            assert offset == 16
            return value.cls
        with self.lock:
            return value[offset // 8]

    def load_i32(self, value, offset):
        if isinstance(value, _Class):
            assert value.alive
            if offset == PYOBJECTHEADER_FLAGS_OFFSET:
                return value.flags
            if offset == 8:
                return PY_TYPE_CLASS
            assert offset == 40
            return len(value.mro)
        if isinstance(value, _Exception):
            assert offset == 8
            return PY_TYPE_EXC
        return value[offset // 4]

    def store_ptr(self, value, offset, item):
        with self.lock:
            value[offset // 8] = item

    def store_i32(self, value, offset, item):
        assert offset == PYOBJECTHEADER_FLAGS_OFFSET
        value.flags = item

    def atomic_load(self, value, offset, _order):
        with self.lock:
            item = value[offset // 8]
            return 0 if item is None else id(item)

    def atomic_cas(self, value, offset, expected, desired, _order, _fail_order):
        with self.lock:
            old = self.atomic_load(value, offset, "acquire")
            if old == expected:
                candidate = self.objects[desired]
                assert candidate.alive and candidate.flags & PY_FLAG_IMMORTAL
                assert candidate.mro[0] is candidate
                value[offset // 8] = candidate
            return old

    def atomic_rmw(self, operation, value, offset, operand, _order):
        assert offset == PYOBJECTHEADER_FLAGS_OFFSET
        with self.lock:
            old = value.flags
            if operation == "and":
                assert any(slot[0] is value for slot in self.roots.values())
                value.flags &= operand
            else:
                assert operation == "or"
                value.flags |= operand
            return old

    def malloc(self, size):
        memory = [None] * (size // 8)
        with self.lock:
            self.allocations[id(memory)] = memory
        return memory

    def free(self, memory):
        if memory is not None:
            with self.lock:
                del self.allocations[id(memory)]

    def frame_enter(self, frame_map, slot):
        assert frame_map == 1
        with self.lock:
            self.roots[id(slot)] = slot

    def store_root(self, slot, value):
        assert value is None
        with self.lock:
            assert id(slot) in self.roots
            candidate = slot[0]
            assert candidate not in self.cache
            assert not candidate.flags & PY_FLAG_IMMORTAL
            slot[0] = None
            candidate.alive = False
            self.retired.append(candidate)

    def frame_leave(self, slot):
        assert slot[0] is None
        with self.lock:
            del self.roots[id(slot)]

    def namespace(self):
        return {
            "C_POINTER_SIZE": 8,
            "PYOBJECTHEADER_FLAGS_OFFSET": PYOBJECTHEADER_FLAGS_OFFSET,
            "PY_FLAG_IMMORTAL": PY_FLAG_IMMORTAL,
            "PYCLASSOBJECT_MRO_OFFSET": PYCLASSOBJECT_MRO_OFFSET,
            "PYINSTANCEOBJECT_CLS_OFFSET": 16,
            "PY_TYPE_CLASS": PY_TYPE_CLASS,
            "PY_TYPE_EXC": PY_TYPE_EXC,
            "PY_TYPE_INT": -1,
            "PY_TYPE_INSTANCE": -2,
            "PY_TYPE_USER_CLASS_START": 65536,
            "c_abi_export": lambda _name: lambda function: function,
            "global_addr": self.globals.__getitem__,
            "load_ptr": self.load_ptr,
            "load_i32": self.load_i32,
            "store_ptr": self.store_ptr,
            "store_i32": self.store_i32,
            "atomic_load_i64": self.atomic_load,
            "atomic_cas_i64": self.atomic_cas,
            "atomic_rmw_i32": self.atomic_rmw,
            "ptr_to_int": id,
            "int_to_ptr": lambda bits: None if bits == 0 else self.objects[bits],
            "ptr_is_null": lambda value: value is None,
            "ptr_eq": lambda first, second: first is second,
            "null": lambda: None,
            "malloc": self.malloc,
            "free": self.free,
            "stack_alloc": lambda size: [None] * (size // 8),
            "py_class_new": self.class_new,
            "pcc_gc_frame_enter": self.frame_enter,
            "pcc_gc_frame_leave": self.frame_leave,
            "pcc_gc_store_root": self.store_root,
            "pcc_gc_load_ptr": lambda _owner, pointer: self.load_ptr(*pointer)
                if isinstance(pointer, tuple) else self.load_ptr(pointer, 0),
            "pcc_gc_note_relocation_read": lambda value: value,
            "ptr_add": lambda value, offset: (value, offset),
            "is_tagged_int": lambda _value: False,
        }


def _load_port_functions(path, namespace):
    parsed = ast.parse(path.read_text(), filename=str(path))
    functions = [node for node in parsed.body if isinstance(node, ast.FunctionDef)]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)


def _concurrent_cold_lookup(source, leaf, workers=8):
    memory = _ColdMemory(workers, leaf)
    namespace = memory.namespace()
    _load_port_functions(source, namespace)
    _load_port_functions(PORT_DIR / "py_exc_match.py", namespace)
    lookup = namespace["py_exc_builtin_class"]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(lookup, leaf) for _ in range(workers)]
        results = [future.result(timeout=10) for future in futures]
    return memory, namespace, results


@pytest.mark.parametrize("leaf", [7, 8])
def test_cold_concurrent_publication_preserves_identity_mro_and_owners(leaf):
    memory, namespace, results = _concurrent_cold_lookup(PORT, leaf)
    canonical = memory.cache[leaf]
    assert all(result is canonical for result in results)
    assert canonical.mro == [canonical, memory.cache[1], memory.cache[0]]
    for result in results:
        error = _Exception(result)
        for expected in canonical.mro:
            assert namespace["py_exc_matches"](error, expected) == 1
    winners = [memory.cache[tag] for tag in (0, 1, leaf)]
    assert all(cls.alive and cls.flags & PY_FLAG_IMMORTAL for cls in winners)
    assert len(memory.retired) == len(memory.created) - len(winners)
    assert not memory.roots
    assert not memory.allocations
    created = len(memory.created)
    assert namespace["py_exc_builtin_class"](leaf) is canonical
    assert namespace["py_exc_builtin_class"](-1) is memory.cache[1]
    assert namespace["py_exc_builtin_class"](65) is memory.cache[1]
    assert len(memory.created) == created


class _MovingMemory(_ColdMemory):
    """Poison moved addresses; rewrite only actual registered slots/cache.

    Constructor collisions still create a completed class. Frame entry,
    the compiler's publication-entry poll, temp-array free and disposal can
    all safepoint. No pinning or name-based matching substitutes for roots.
    """

    def __init__(self, phase):
        super().__init__(1, 8)
        self.phase = phase
        self.moves = 0
        root = _Class("BaseException", None)
        parent = _Class("Exception", root)
        self.cache[0], self.cache[1] = root, parent
        self.objects.update({id(root): root, id(parent): parent})

    def move(self, old):
        assert old.alive
        replacement = _Class(old.name, None)
        replacement.flags = old.flags
        replacement.mro = old.mro.copy()
        self.objects[id(replacement)] = replacement
        for cls in list(self.objects.values()):
            if cls.alive:
                cls.mro = [replacement if entry is old else entry for entry in cls.mro]
        self.cache = [replacement if item is old else item for item in self.cache]
        self.globals["py_exc_classes"] = self.cache
        for slot in self.roots.values():
            if slot[0] is old:
                slot[0] = replacement
        self.created = [replacement if cls is old else cls for cls in self.created]
        old.alive = False
        self.moves += 1
        return replacement

    def class_new(self, name, bases, n_bases, fields, n_fields):
        candidate = super().class_new(name, bases, n_bases, fields, n_fields)
        # Complete a competing publication after the accessor's cold read.
        winner = _Class(name, bases[0] if n_bases else None)
        self.objects[id(winner)] = winner
        self.cache[8] = winner
        return candidate

    def frame_enter(self, frame_map, slot):
        super().frame_enter(frame_map, slot)
        if self.phase == "frame_enter":
            self.move(slot[0])

    def free(self, memory):
        if memory is not None and self.phase == "free_bases":
            assert len(self.roots) == 1, "candidate must be rooted before temporary-base free can park"
            self.move(next(iter(self.roots.values()))[0])
        super().free(memory)

    def store_root(self, slot, value):
        if self.phase == "root_store":
            self.move(slot[0])
            self.move(self.cache[8])
        super().store_root(slot, value)

    def frame_leave(self, slot):
        super().frame_leave(slot)
        if self.phase == "frame_leave":
            self.move(self.cache[8])


@pytest.mark.parametrize("phase", [
    "frame_enter", "free_bases", "publication_entry", "root_store", "frame_leave",
])
def test_losing_candidate_reloads_healed_roots_and_returns_current_cache(phase):
    memory = _MovingMemory(phase)
    namespace = memory.namespace()
    _load_port_functions(PORT, namespace)
    original_publish = namespace["_exc_cache_publish"]
    if phase == "publication_entry":
        def publish_after_compiler_poll(tag, candidate_slot):
            assert id(candidate_slot) in memory.roots, "publish entry needs an already registered root"
            memory.move(candidate_slot[0])
            return original_publish(tag, candidate_slot)
        namespace["_exc_cache_publish"] = publish_after_compiler_poll
    result = namespace["py_exc_builtin_class"](8)
    assert result is memory.cache[8] and result.alive
    assert result.mro[0] is result
    assert memory.moves >= 1
    assert len(memory.retired) == 1 and not memory.retired[0].alive
    assert not memory.roots and not memory.allocations
    assert result.flags & PY_FLAG_IMMORTAL


@pytest.mark.parametrize("phase", ["frame_enter", "free_bases", "frame_leave"])
def test_winning_candidate_transfers_owner_and_reloads_after_frame_leave(phase):
    memory = _MovingMemory(phase)
    memory.class_new = lambda *args: _ColdMemory.class_new(memory, *args)
    namespace = memory.namespace()
    _load_port_functions(PORT, namespace)
    result = namespace["py_exc_builtin_class"](8)
    assert result is memory.cache[8] and result.alive
    assert result.mro[0] is result
    assert memory.moves >= 1
    assert not memory.retired  # Clearing the transferred slot never releases cache's owner.
    assert not memory.roots and not memory.allocations
    assert result.flags & PY_FLAG_IMMORTAL


@pytest.mark.parametrize("triple", [
    "arm64-apple-darwin", "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
])
def test_exception_cache_publication_reaches_owned_emitter(tmp_path, monkeypatch, triple):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "py_runtime_cache" / "py" / "py_exc_table.py"
    source.parent.mkdir(parents=True)
    source.write_text(PORT.read_text())
    output = tmp_path / "py_exc_table.ll"
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    # Runtime emission needs its normal pointer-carrier promotion before
    # precise stack-map analysis can resolve an explicitly registered slot.
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", runtime_ir_passes(str(PORT_DIR.parent)))
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple=triple)
    ir_text = output.read_text()
    assert "load atomic i64" in ir_text and "acquire" in ir_text
    assert "cmpxchg ptr" in ir_text and "acq_rel acquire" in ir_text
    assert "cmpxchg weak" not in ir_text
    assert "atomicrmw and" in ir_text
    publish = re.search(
        r"^define [^\n]*@user_py_exc_table__exc_cache_publish\([^\n]*\n(.*?)^}",
        ir_text, re.M | re.S,
    )
    assert publish is not None
    body = publish.group(1)
    assert "@pcc_gc_frame_enter(" not in body  # Root exists before the implicit entry poll.
    assert len(re.findall(r"\bcall [^\n]*@pcc_gc_load_ptr\(", body)) >= 2
    assert "inttoptr" not in body  # A raw old CAS observation cannot survive cleanup.
    assert body.index("@pcc_gc_frame_leave(") < body.index("@user_py_exc_table__exc_cache_get(")
    payload = emit_owned_object(ir_text, triple)
    assert len(payload) > 64
    (tmp_path / "py_exc_table.o").write_bytes(payload)


PROGRAM = '''from threading import Thread
from pcc.extern import extern, c_int64, c_rawptr
from pcc.unsafe import malloc, free, atomic_load_i64, atomic_store_i64, atomic_rmw_i64

lookup = extern("py_exc_builtin_class", (c_int64,), c_rawptr)
cached = extern("py_subs_exc_cache_get", (c_int64,), c_rawptr)
workers = 16
control = malloc(16)
atomic_store_i64(control, 0, 0, "release")
atomic_store_i64(control, 8, 0, "release")
seen = [0] * workers
matched = [False] * workers

def worker(index):
    atomic_rmw_i64("add", control, 0, 1, "acq_rel")
    while atomic_load_i64(control, 8, "acquire") == 0:
        pass
    first = lookup(8)
    error = StopIteration()
    matched[index] = (type(error) is StopIteration and isinstance(error, Exception)
                      and isinstance(error, BaseException))
    seen[index] = first

def main():
    assert cached(8) == 0
    threads = []
    index = 0
    while index < workers:
        thread = Thread(target=worker, args=(index,))
        threads.append(thread)
        thread.start()
        index = index + 1
    while atomic_load_i64(control, 0, "acquire") < workers:
        pass
    # Even if the caller is continuation-lifted, preparing the workers must
    # not exhaust an iterator and warm StopIteration before releasing them.
    assert cached(8) == 0
    atomic_store_i64(control, 8, 1, "release")
    for thread in threads:
        thread.join()
    canonical = lookup(8)
    assert canonical != 0
    for observed in seen:
        assert observed == canonical
    for accepted in matched:
        assert accepted
    free(control)
    print("COLD_EXCEPTION_CACHE_OK")
main()
'''


@pytest.mark.integration
def test_native_cold_exception_identity_and_matching_all_gcs(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive, python_program_compiler,
):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    source = tmp_path / "cold_exception_cache.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "cold_exception_cache"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive))
    import os
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "COLD_EXCEPTION_CACHE_OK\n", (backend, result.stdout)
