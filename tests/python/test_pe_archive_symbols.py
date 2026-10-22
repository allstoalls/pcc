"""Bounded host PE archive controls; no native execution or timing claim."""

from dataclasses import replace
import itertools

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import pe_x86_64 as pe
from pcc.backend.ar_writer import _member, write_archive


def _object(name, needs=(), *, aliases=(), local=(), tag=b"", tls_index=False):
    sections = [coff.CoffSection(".text", b"\xc3", 0x60000020, 1)]
    if tag:
        sections.append(coff.CoffSection(".rdata", tag, 0x40000040, 1))
    symbols = [coff.CoffSymbol(value, 1) for value in (name, *aliases)]
    symbols.extend(coff.CoffSymbol(value, 0) for value in needs)
    symbols.extend(coff.CoffSymbol(value, 1, external=False) for value in local)
    if tls_index:
        sections.append(coff.CoffSection(".data$tlsidx", b"\0" * 4, 0xC0000040, 4))
        symbols.append(coff.CoffSymbol("_tls_index", len(sections)))
    return coff.CoffObject(tuple(sections), tuple(symbols))


def _archive(objects):
    return write_archive([(f"member-{index}.o", coff.emit_object(obj))
                          for index, obj in enumerate(objects)])


def _reference_symbols(objects):
    # Frozen symbol and extraction policy from ba93191f / pe blob 1583e126.
    # Keep this independent of the incremental registration helper.
    defined = {}
    needed = set()
    for oi, obj in enumerate(objects):
        for si, symbol in enumerate(obj.symbols):
            if not symbol.external:
                continue
            if symbol.section:
                if symbol.name in defined:
                    raise coff.CoffError("duplicate COFF symbol: " + symbol.name)
                defined[symbol.name] = (oi, si)
            else:
                needed.add(symbol.name)
    return defined, needed - set(defined)


def _reference_link(objects, archives=(), entry="pcc_windows_start"):
    objects = list(objects)
    pool = []
    for archive in archives:
        pool.extend(coff.parse_object(payload)
                    for _, payload in pe.read_archive_payloads(archive))
    while True:
        definitions, needed = _reference_symbols(objects)
        if entry not in definitions:
            needed.add(entry)
        selected = -1
        for index, obj in enumerate(pool):
            if any(sym.external and sym.section and sym.name in needed for sym in obj.symbols):
                selected = index
                break
        if selected < 0:
            break
        objects.append(pool.pop(selected))
    definitions, needed = _reference_symbols(objects)
    if entry not in definitions:
        raise coff.CoffError("Windows entry is not defined: " + entry)
    if "_tls_index" not in definitions:
        objects.append(coff.CoffObject(
            (coff.CoffSection(".data$tlsidx", b"\0" * 4, 0xC0000040, 4),),
            (coff.CoffSymbol("_tls_index", 1),),
        ))
    definitions, needed = _reference_symbols(objects)
    boundaries = {"__init_array_start", "__init_array_end",
                  "__fini_array_start", "__fini_array_end"}
    if boundaries & set(definitions):
        raise coff.CoffError("initializer boundary symbols are owned by the linker")
    needed -= boundaries
    imports = {name[6:] if name.startswith("__imp_") else name for name in needed}
    for name in imports:
        pe.system_dll(name)
    # The selected-object emitter is unchanged by this patch. Compare the
    # complete image produced from the frozen extraction policy, not headers.
    return pe._link_selected_objects(objects, definitions, imports, boundaries,
                                     entry, 0x140000000, None)


def _outcome(link, objects, archives=(), entry="pcc_windows_start"):
    try:
        return link(objects, archives=archives, entry=entry)
    except coff.CoffError as exc:
        return type(exc), str(exc)


@pytest.mark.parametrize("order", tuple(itertools.permutations(range(3))))
@pytest.mark.parametrize("split_archives", (False, True))
def test_transitive_archive_order_matches_frozen_policy(order, split_archives):
    main = _object("pcc_windows_start", ("first",), tag=b"main")
    members = (
        _object("first", ("last",), tag=b"first"),
        _object("last", ("first",), tag=b"last"),
        _object("unused", ("not_a_system_import",), tag=b"unused"),
    )
    ordered = [members[index] for index in order]
    archives = ([_archive([obj]) for obj in ordered] if split_archives
                else [_archive(ordered)])
    original = coff.emit_object(main)
    expected = _reference_link([main], archives)
    actual = pe.link_executable([main], archives=archives)
    assert actual == expected
    assert b"unused" not in actual
    assert coff.emit_object(main) == original


@pytest.mark.parametrize("mode", (
    "entry-in-archive", "first-definition", "backward-activation", "local-definition",
    "defined-then-undefined", "undefined-then-defined", "explicit-tls-index",
    "imports-and-boundaries", "multiple-missing-imports", "missing-entry",
    "duplicate-initial", "duplicate-member", "duplicate-inside-member", "owned-boundary",
))
def test_archive_images_and_first_errors_match_frozen_policy(mode):
    main = _object("pcc_windows_start", ("wanted",), tag=b"main")
    objects = [main]
    members = [_object("wanted", tag=b"winner")]
    if mode == "entry-in-archive":
        objects, members = [], [_object("pcc_windows_start", tag=b"entry")]
    elif mode == "first-definition":
        members.append(_object("wanted", ("bad_unselected_import",), tag=b"loser"))
    elif mode == "backward-activation":
        members = [_object("earlier", tag=b"earlier"),
                   _object("wanted", ("earlier",), tag=b"later")]
    elif mode == "local-definition":
        members.insert(0, _object("unused", local=("wanted",), tag=b"local"))
    elif mode in ("defined-then-undefined", "undefined-then-defined"):
        extra = (coff.CoffSymbol("same", 1), coff.CoffSymbol("same", 0))
        if mode == "undefined-then-defined":
            extra = extra[::-1]
        objects = [replace(main, symbols=main.symbols + extra)]
        members.append(_object("same", tag=b"not selected"))
    elif mode == "explicit-tls-index":
        objects = [_object("pcc_windows_start", ("wanted",), tls_index=True)]
    elif mode == "imports-and-boundaries":
        objects = [_object("pcc_windows_start", ("ExitProcess", "__imp_ExitProcess",
                                               "__init_array_start", "__fini_array_end"))]
        members = []
    elif mode == "multiple-missing-imports":
        objects = [_object("pcc_windows_start", ("unknown_a", "unknown_b", "unknown_c"))]
        members = []
    elif mode == "missing-entry":
        objects, members = [_object("other")], []
    elif mode == "duplicate-initial":
        objects.append(_object("pcc_windows_start"))
    elif mode == "duplicate-member":
        members = [_object("wanted", aliases=("pcc_windows_start",))]
    elif mode == "duplicate-inside-member":
        members = [_object("wanted", aliases=("duplicate", "duplicate"))]
    elif mode == "owned-boundary":
        objects = [_object("pcc_windows_start", aliases=("__init_array_start",))]
    archives = [_archive(members)]
    expected = _outcome(_reference_link, objects, archives)
    error_modes = {"multiple-missing-imports", "missing-entry", "duplicate-initial",
                   "duplicate-member", "duplicate-inside-member", "owned-boundary"}
    assert isinstance(expected, tuple if mode in error_modes else bytes)
    assert _outcome(pe.link_executable, objects, archives) == expected


def test_unused_archive_parse_still_precedes_initial_duplicate_check():
    objects = [_object("pcc_windows_start"), _object("pcc_windows_start")]
    # The indexed writer rejects malformed objects itself; use its framing
    # helper to exercise the linker's eager parse order, not writer validation.
    archives = [b"!<arch>\n" + _member("bad.o", b"not a COFF header", 8)]
    expected = _outcome(_reference_link, objects, archives)
    assert expected == (coff.CoffError, "truncated COFF header")
    assert _outcome(pe.link_executable, objects, archives) == expected


def test_each_selected_object_is_registered_once_before_budget_prepare(monkeypatch):
    from pcc.backend.pe_host_budget import PEHostBudget

    count = 8
    main = _object("pcc_windows_start", ("worker_0",))
    members = [_object(f"worker_{index}",
                       (f"worker_{index + 1}",) if index + 1 < count else ())
               for index in reversed(range(count))]
    archives = [_archive(members)]
    expected = _reference_link([main], archives)
    visits = []
    register = pe._add_symbols

    def record(obj, oi, defined, needed):
        visits.append((oi, len(obj.symbols)))
        return register(obj, oi, defined, needed)

    class ObservedBudget(PEHostBudget):
        def prepare(self, objects, imports):
            assert [row[0] for row in visits] == list(range(count + 2))
            assert len(objects) == count + 2 and not imports
            assert objects[-1].symbols[0].name == "_tls_index"
            return super().prepare(objects, imports)

    monkeypatch.setattr(pe, "_add_symbols", record)
    budget = ObservedBudget(10_000_000, 1000, set_header_bytes=10,
                            set_entry_bytes=2, integer_bytes=3,
                            metadata_entry_bytes=7, allocation_quantum=1)
    assert pe.link_executable([main], archives=archives, compact_budget=budget) == expected
    assert len(visits) == count + 2
    assert sum(size for _oi, size in visits) == 2 * count + 2
    assert budget.retained_bytes == 0 and not budget._buffers


def test_symbol_snapshot_helper_keeps_existing_contract():
    objects = [_object("pcc_windows_start", ("later", "ExitProcess"), local=("private",)),
               _object("later", ("pcc_windows_start",))]
    assert pe._symbols(objects) == _reference_symbols(objects)
