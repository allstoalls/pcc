"""Tiny CI-only final-image controls; no ELF execution or compiler invocation.

This source-only candidate has not run these tests. Normal CI must select them
explicitly before any correctness or performance claim is made.
"""

import struct
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend.owned_elf_inputs import ElfInputStore
from tests.elf_link_image_preimage import original_linker


BASE = 0x40000000
FAR = 0x123456788
TARGET = "aarch64-unknown-linux-gnu"


def _words(*words):
    return b"".join(struct.pack("<I", word) for word in words)


def _pack(rows):
    return b"".join(elf._RELA.pack(offset, (symbol << 32) | kind, addend)
                    for offset, symbol, kind, addend in rows)


def _fixture():
    text_rows = ((0, 2, 283, -12), (4, 2, 283, -12),
                 (8, 2, 311, 0), (12, 2, 312, 0), (16, 0, 0, 0))
    data_rows = ((0, 2, 257, 5),)
    obj = elf.ElfObject(
        (elf.ElfSection(".text", 1, 6, 8,
                        _words(0x94000000, 0x94000000, 0x90000010,
                               0xF9400210, 0xD503201F, 0xD65F03C0),
                        relocations=tuple(elf.ElfRelocation(*row)
                                          for row in text_rows)),
         elf.ElfSection(".data", 1, 3, 8, bytes(8),
                        relocations=tuple(elf.ElfRelocation(*row)
                                          for row in data_rows))),
        (elf.ElfSymbol.null(),
         elf.ElfSymbol("_start", 1, 0, 24, 1, elf.STT_FUNC),
         elf.ElfSymbol("target", elf.SHN_ABS, FAR, 0, 1, elf.STT_FUNC)),
        elf.EM_AARCH64,
    )
    return obj, text_rows, data_rows


def _packed_object(rows=None):
    obj, text_rows, data_rows = _fixture()
    if rows is not None:
        text_rows = rows
    for section, section_rows in zip(obj.sections, (text_rows, data_rows)):
        object.__setattr__(section, "relocations",
                           elf._PackedElfRelocations(_pack(section_rows)))
    return obj


def _exception_chain(exc):
    """Bounded semantic signature; explicit cause wins over visible context."""
    chain = []
    seen = set()
    current = exc
    for _ in range(8):
        if id(current) in seen:
            chain.append(("cycle", type(current), str(current)))
            return tuple(chain)
        seen.add(id(current))
        next_exc = current.__cause__
        edge = "cause" if next_exc is not None else None
        if next_exc is None and not current.__suppress_context__:
            next_exc = current.__context__
            edge = "context" if next_exc is not None else None
        chain.append((type(current), str(current),
                      bool(current.__suppress_context__), edge))
        if next_exc is None:
            return tuple(chain)
        current = next_exc
    chain.append(("truncated", type(current), str(current)))
    return tuple(chain)


def _outcome(link, obj, **kwargs):
    try:
        return ("image", bytes(link([obj], base_address=BASE, **kwargs)))
    except Exception as exc:
        return (type(exc), str(exc), _exception_chain(exc))


def test_staged_final_image_matches_reference_and_removes_packed_records(tmp_path, monkeypatch):
    obj, text_rows, data_rows = _fixture()
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(obj))
    store = ElfInputStore(TARGET)
    staged = store.stage(str(path))
    expected_rows = len(text_rows) + len(data_rows)
    constructor = elf.ElfRelocation.__init__
    calls = []

    def counted(self, *args, **kwargs):
        calls.append(1)
        constructor(self, *args, **kwargs)

    monkeypatch.setattr(elf.ElfRelocation, "__init__", counted)
    before = _outcome(original_linker(elf), staged, _input_store=store)
    before_calls = len(calls)
    calls.clear()
    after = _outcome(elf._link_static_executable_image, staged, _input_store=store)
    after_calls = len(calls)
    assert after == before and after[0] == "image"
    # Staging ended before counting. No archives or public re-validation are
    # included, and every nonempty alloc relocation range is exact packed.
    # The candidate now avoids projections in classification and application.
    assert (before_calls, after_calls) == (2 * expected_rows, 0)
    assert path.read_bytes() == elf.emit_relocatable(obj)
    store.verify()


@pytest.mark.parametrize("rows", [
    ((0, 2, 283, 2),),
    ((0, 2, 283, -(1 << 63)),),
    ((0, 99, 311, 0), (4, 2, 283, 2)),
    ((0, 2, 283, 2), (4, 99, 283, 0)),
])
def test_actual_final_image_first_error_matches_reference(rows):
    # Deliberate post-validation malformed records exercise the final-link
    # boundary itself. Normal staged files reject invalid indices earlier.
    before = _outcome(original_linker(elf), _packed_object(rows))
    after = _outcome(elf._link_static_executable_image, _packed_object(rows))
    assert after == before and after[0] != "image"


@pytest.mark.parametrize("shape", ["list", "fresh-custom", "packed-subclass", "custom-iterator"])
def test_generic_iteration_field_and_temporary_lifetime_order_is_unchanged(shape):
    def run(link):
        obj, text_rows, _data_rows = _fixture()
        events = []

        class Row:
            def __init__(self, values):
                self.values = dict(zip(("offset", "symbol_index", "type", "addend"), values))

            def __getattr__(self, name):
                events.append(("field", name))
                return self.values[name]

        rows = [Row(row) for row in text_rows]

        class Fresh:
            def __iter__(self):
                events.append(("iter",))
                return iter(rows)

            def __del__(self):
                events.append(("released",))

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    events.append(("relocations",))
                    return Fresh()
                return getattr(self.source, name)

        class PackedSubclass(elf._PackedElfRelocations):
            def __iter__(self):
                events.append(("packed-subclass-iter",))
                return iter(rows)

        class Cursor:
            def __init__(self):
                self.index = 0

            def __iter__(self):
                raise AssertionError("the returned iterator must not be iterated again")

            def __next__(self):
                if self.index == len(rows):
                    raise StopIteration
                row = rows[self.index]
                self.index += 1
                return row

        class CustomIterable:
            def __iter__(self):
                events.append(("custom-iterable",))
                return Cursor()

        if shape == "fresh-custom":
            object.__setattr__(obj, "sections", (Section(obj.sections[0]), obj.sections[1]))
        else:
            value = (rows if shape == "list" else CustomIterable()
                     if shape == "custom-iterator" else PackedSubclass(_pack(text_rows)))
            object.__setattr__(obj.sections[0], "relocations", value)
        result = _outcome(link, obj)
        return result, events

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before and after[0][0] == "image"


@pytest.mark.parametrize("shape", ["packed", "generic"])
def test_classification_exception_traceback_does_not_retain_temporary_inputs(shape):
    def run(link):
        obj, _text_rows, _data_rows = _fixture()
        views = []
        bad_row = (8, 99, 311, 0)

        class Cursor:
            def __init__(self):
                self.done = False

            def __iter__(self):
                return self

            def __next__(self):
                if self.done:
                    raise StopIteration
                self.done = True
                return elf.ElfRelocation(*bad_row)

        class Fresh:
            def __iter__(self):
                cursor = Cursor()
                views.append(weakref.ref(cursor))
                return cursor

        class Section:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                if name == "relocations":
                    view = (elf._PackedElfRelocations(_pack((bad_row,)))
                            if shape == "packed" else Fresh())
                    views.append(weakref.ref(view))
                    return view
                return getattr(self.source, name)

        object.__setattr__(obj, "sections", (Section(obj.sections[0]), obj.sections[1]))
        saved = None
        try:
            link([obj], base_address=BASE)
        except Exception as exc:
            saved = exc
        assert saved is not None
        # Keep the full exception/traceback alive while observing the views.
        result = (type(saved), str(saved), _exception_chain(saved),
                  tuple(view() is not None for view in views))
        assert saved.__traceback__ is not None
        return result

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before and before[0] is IndexError
    assert before[3] and not any(before[3])


def test_classification_reloads_payload_after_symbol_callback(monkeypatch):
    original_identity = elf._symbol_identity

    def run(use_reference):
        obj = _packed_object(((8, 2, 311, 0), (0, 2, 283, -12)))
        packed = obj.sections[0].relocations
        replacement = _pack(((8, 2, 311, 0), (0, 2, 283, -16)))
        events = []

        def identity(*args):
            events.append("identity")
            if len(events) == 1:
                object.__setattr__(packed, "payload", replacement)
            return original_identity(*args)

        with monkeypatch.context() as patch:
            patch.setattr(elf, "_symbol_identity", identity)
            link = original_linker(elf) if use_reference else elf._link_static_executable_image
            result = _outcome(link, obj)
        return result, events

    before = run(True)
    after = run(False)
    assert after == before and after[0][0] == "image"


def test_preceding_custom_record_is_released_at_first_packed_row(monkeypatch):
    def run(use_reference):
        obj, text_rows, data_rows = _fixture()
        events = []
        rela = elf._RELA

        class Row:
            def __init__(self, values):
                self.offset, self.symbol_index, self.type, self.addend = values

            def __del__(self):
                events.append("row-released")

        class Rows:
            def __iter__(self):
                for row in text_rows:
                    yield Row(row)

        class Rela:
            size = rela.size

            def unpack_from(self, payload, offset):
                events.append("packed-unpack")
                return rela.unpack_from(payload, offset)

        object.__setattr__(obj.sections[0], "relocations", Rows())
        object.__setattr__(obj.sections[1], "relocations",
                           elf._PackedElfRelocations(_pack(data_rows)))
        with monkeypatch.context() as patch:
            patch.setattr(elf, "_RELA", Rela())
            link = original_linker(elf) if use_reference else elf._link_static_executable_image
            result = _outcome(link, obj)
        return result, events

    before = run(True)
    after = run(False)
    assert after == before and after[0][0] == "image"


@pytest.mark.parametrize("change", ["append", "truncate"])
def test_staged_source_identity_failure_keeps_original_boundary(tmp_path, change):
    obj, _text, _data = _fixture()
    encoded = elf.emit_relocatable(obj)
    path = tmp_path / "input.o"
    outcomes = []
    for link in (original_linker(elf), elf._link_static_executable_image):
        path.write_bytes(encoded)
        store = ElfInputStore(TARGET)
        staged = store.stage(str(path))
        path.write_bytes(encoded + b"x" if change == "append" else encoded[:-1])
        outcomes.append(_outcome(link, staged, _input_store=store))
    assert outcomes[0] == outcomes[1] and outcomes[0][0] == elf.ElfError


def test_packed_view_does_not_survive_until_next_source_load(tmp_path, monkeypatch):
    def run(link):
        objects = []
        store = ElfInputStore(TARGET)
        for index in range(2):
            obj, _text, _data = _fixture()
            if index:
                symbols = tuple(elf.ElfSymbol(
                    symbol.name + "2" if symbol.name else "", symbol.section_index,
                    symbol.value, symbol.size, symbol.binding, symbol.type,
                    symbol.visibility) for symbol in obj.symbols)
                object.__setattr__(obj, "symbols", symbols)
            path = tmp_path / (str(index) + ".o")
            path.write_bytes(elf.emit_relocatable(obj))
            objects.append(store.stage(str(path)))
        views = []
        transitions = []
        post_init = elf._PackedElfRelocations.__post_init__
        load = store.load

        def watched_view(self):
            post_init(self)
            views.append(weakref.ref(self))

        def checked_load(index):
            if store.current_index != index:
                transitions.append(sum(view() is not None for view in views))
            return load(index)

        with monkeypatch.context() as patch:
            patch.setattr(elf._PackedElfRelocations, "__post_init__", watched_view)
            patch.setattr(store, "load", checked_load)
            image = bytes(link(objects, base_address=BASE, _input_store=store))
        return image, transitions

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before
    assert before[1] and all(count == 0 for count in before[1])


def test_tiny_same_input_final_link_alternating_raw_receipt(tmp_path):
    # Normal-CI-only observation. This test never runs an emitted image, starts
    # a process, profiles, changes GC configuration, or asserts a speedup.
    import __future__
    import ast
    import gc
    import hashlib
    import json
    import os
    from pathlib import Path
    import sys
    import time

    from tests.elf_link_image_preimage import ORIGINAL_LINK_IMAGE_SOURCE

    obj, text_rows, data_rows = _fixture()
    for section, rows in zip(obj.sections, (text_rows, data_rows)):
        object.__setattr__(section, "relocations", tuple(
            elf.ElfRelocation(*row) for row in rows * 256))
    encoded = elf.emit_relocatable(obj)
    path = tmp_path / "timing-input.o"
    path.write_bytes(encoded)
    store = ElfInputStore(TARGET)
    staged = store.stage(str(path))
    packed_rows = sum(section.relocation_count for section in staged.sections)
    assert packed_rows == 1536
    def timing_linkers():
        # Both measured functions start with fresh code objects and independent
        # copies of one module snapshot. Do not replace the live module owner.
        root = Path(__file__).resolve().parents[2]
        owner_path = root / "pcc/backend/elf_x86_64.py"
        live = elf._link_static_executable_image
        assert Path(elf.__file__).resolve() == owner_path.resolve()
        assert Path(live.__code__.co_filename).resolve() == owner_path.resolve()
        assert live.__module__ == elf.__name__
        assert live.__name__ == live.__code__.co_name == "_link_static_executable_image"
        raw_owner = owner_path.read_bytes()
        owner_text = raw_owner.decode("utf-8").replace("\r\n", "\n")
        assert "\r" not in owner_text
        tree = ast.parse(owner_text)
        owners = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == live.__name__]
        assert len(owners) == 1 and not owners[0].decorator_list
        owner = owners[0]
        assert live.__code__.co_firstlineno == owner.lineno
        candidate_source = "".join(owner_text.splitlines(keepends=True)[
            owner.lineno - 1:owner.end_lineno])
        original_source = ORIGINAL_LINK_IMAGE_SOURCE.replace("\r\n", "\n")
        assert "\r" not in original_source
        flags = __future__.annotations.compiler_flag
        assert live.__code__.co_flags & flags == flags
        assert any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                   and any(alias.name == "annotations" for alias in node.names) for node in tree.body)
        assert hashlib.sha256(original_source.encode("utf-8")).hexdigest() == (
            "094f3b9e02b8e6c7e6afaa0ea8bd1dff75cb8dd304594d117511b715e0a6ba59")
        snapshot = dict(vars(elf))
        linkers = {}
        for name, source in (("original", original_source), ("candidate", candidate_source)):
            namespace = dict(snapshot)
            code = compile(source, "<fresh timing " + name + " final-image linker>", "exec",
                           flags=flags, dont_inherit=True, optimize=sys.flags.optimize)
            exec(code, namespace, namespace)
            linkers[name] = namespace[live.__name__]
            assert linkers[name] is not live
            assert linkers[name].__globals__ is namespace
            assert linkers[name].__code__.co_flags & flags == flags
        assert linkers["original"].__globals__ is not linkers["candidate"].__globals__
        assert elf._link_static_executable_image is live
        preparation = {
            "schema": "fresh-source-both.v1", "warmup_calls": 0,
            "method": "fresh compile/exec once per variant; reused four times each",
            "globals": "independent copies of one dict(vars(elf)) snapshot; globals=locals",
            "compile_flags": flags, "dont_inherit": True, "optimize": sys.flags.optimize,
            "function_source": "exact AST line slice; UTF-8 with CRLF normalized to LF",
            "candidate_source_lines": [owner.lineno, owner.end_lineno],
            "owner_raw_sha256": hashlib.sha256(raw_owner).hexdigest(),
        }
        return linkers, preparation, hashlib.sha256(candidate_source.encode("utf-8")).hexdigest()

    linkers, preparation, candidate_function_sha256 = timing_linkers()
    orders = (("original", "candidate"), ("candidate", "original"),
              ("original", "candidate"), ("candidate", "original"))
    rounds = []
    expected = None
    gc_enabled = gc.isenabled()
    gc_threshold = gc.get_threshold()
    for round_index, order in enumerate(orders):
        for name in order:
            store.clear()
            gc_before = gc.get_count()
            started = time.perf_counter_ns()
            image = linkers[name]([staged], base_address=BASE, _input_store=store)
            elapsed = time.perf_counter_ns() - started
            gc_after = gc.get_count()
            if expected is None:
                expected = bytes(image)
            assert image == expected
            # No previous returned mutable image crosses the next timer.
            del image
            rounds.append({"round": round_index, "linker": name,
                           "elapsed_ns": elapsed, "gc_count_before": gc_before,
                           "gc_count_after": gc_after})
    assert len(rounds) == 8 and all(row["elapsed_ns"] >= 0 for row in rounds)
    assert gc.isenabled() == gc_enabled and gc.get_threshold() == gc_threshold
    assert path.read_bytes() == encoded
    store.verify()
    root = Path(__file__).resolve().parents[2]
    source_paths = ("pcc/backend/elf_x86_64.py",
                    "pcc/backend/elf_aarch64_relocations.py",
                    "pcc/backend/owned_elf_inputs.py",
                    "tests/elf_link_image_preimage.py",
                    "tests/python/test_elf_packed_link_classification.py")
    receipt = {
        "schema": "pcc.arm-packed-classification-final-image-ab.v1",
        "scope": "one frozen synthetic staged PCO; cumulative classification/application host link only",
        "input": {"sha256": hashlib.sha256(encoded).hexdigest(),
                  "bytes": len(encoded), "packed_rows": packed_rows,
                  "rela_bytes": packed_rows * 24, "text_bytes": 24, "data_bytes": 8},
        "image_sha256": hashlib.sha256(expected).hexdigest(),
        "source": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in source_paths},
        "original_function_sha256": hashlib.sha256(
            ORIGINAL_LINK_IMAGE_SOURCE.encode()).hexdigest(),
        "candidate_function_sha256": candidate_function_sha256,
        "preparation": preparation,
        "runtime": {"implementation": sys.implementation.name,
                    "version": sys.version, "executable": sys.executable,
                    "executable_sha256": hashlib.sha256(
                        Path(sys.executable).read_bytes()).hexdigest(),
                    "platform": sys.platform,
                    "machine": os.uname().machine if hasattr(os, "uname") else "unknown"},
        "gc": {"enabled": gc_enabled, "threshold": gc_threshold,
               "policy": "unchanged; counts recorded per call; no collection attribution"},
        "rounds": rounds,
        "timing_threshold": None,
        "limits": ("Unwarmed same-process synthetic input; both measured functions are fresh. "
                   "Shared helpers, imports, caches, allocator and GC state are not reset. The first call "
                   "precedes creation of immutable expected bytes retained thereafter. "
                   "Adaptive state evolves across four calls per variant; no cold-global "
                   "or Stage1/production speedup inference."),
    }
    assert receipt["source"]["pcc/backend/elf_x86_64.py"] == preparation["owner_raw_sha256"]
    output = tmp_path / "arm-packed-classification-final-image-ab.json"
    data = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    assert len(data.encode()) <= 8192
    output.write_text(data)
    assert json.loads(output.read_text()) == json.loads(data)
