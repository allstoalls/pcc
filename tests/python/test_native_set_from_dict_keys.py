"""Builtins over a mapping must iterate its KEYS, not index it positionally.

The generic ``set(iterable)`` lowering walked the source with
``py_obj_len`` + ``py_obj_getitem(src, i)``.  For a dict that indexing is a
KEY lookup for 0, 1, 2 …, so a string-keyed mapping produced an EMPTY set
with no error — a silent wrong answer, not a fallback.

It stayed invisible because CPython answers ``frozenset(some_dict)``
natively, so only pcc-compiled code was affected.  Inside pcc1's own
backend that made ``frozenset(managed_origins)`` empty in
``build_function_stack_map_plan``, which tracked no managed SSA values and
therefore emitted **zero** managed-value reloads: emitting one real module
gave 0 reload triples under pcc1 against 1200 under host pcc, for
byte-different assembly from the same IR.

Sweeping the rest of the family from the same probe found three more sites
with the identical defect, two of them equally silent:

    set(d) / frozenset(d)   empty set          (silent)
    dict(d)                 empty dict         (silent)
    any(d) / all(d)         False / False      (silent)
    zip(d, ...)             KeyError           (loud, still wrong)

`list(d)`, `tuple(d)`, `sorted(d)`, comprehensions, `for k in d`,
`enumerate(d)`, `in`, `.keys()/.values()/.items()` and `str.join(d)` were
already correct. `min(d)` / `max(d)` / `sum(d)` are not implemented in the
no-libpython closure and fail closed with a NameError, which is a capability
gap rather than this defect.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


_PROGRAM = """
    def build() -> dict:
        d: dict = {}
        d["a"] = 1
        d["b"] = 2
        d["c"] = 3
        return d


    def main() -> None:
        d = build()
        print(len(frozenset(d)))
        print(len(set(d)))
        print(len(frozenset(d.keys())))
        print(len(list(d)))
        print(sorted(set(d))[0])


    main()
"""


def _compile_to_ll(source: str, name: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    match = pattern.search(ir_text)
    return match.group(1) if match else None


def test_set_from_dict_lowers_through_dict_keys():
    ir_text = _compile_to_ll(
        """
        def f(d: dict) -> object:
            return frozenset(d)
        """,
        "set_from_dict_keys",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@py_dict_keys" in body, body
    # Positional indexing is what produced the empty set; a mapping must
    # never reach it.
    assert "@py_obj_getitem" not in body, body


def test_set_and_frozenset_of_dict_have_dict_length(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(_PROGRAM).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["3", "3", "3", "3", "a"], native.stdout


@pytest.mark.parametrize("call", ["set", "frozenset"])
def test_dict_keys_survive_a_non_string_key(tmp_path, call):
    from pcc.frontends.python.pipeline import compile_python

    # Integer keys are the case the broken lowering could accidentally get
    # right: py_obj_getitem(d, 0) really does find the key 0.  Use keys that
    # are ints but not 0..n-1 so a positional walk still cannot pass.
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            f"""
            def main() -> None:
                d: dict = {{}}
                d[10] = "x"
                d[20] = "y"
                out = {call}(d)
                print(len(out))
                print(10 in out)
                print(0 in out)


            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["2", "True", "False"], native.stdout


_FAMILY = """
    def build() -> dict:
        d: dict = {}
        d["b"] = 2
        d["a"] = 1
        d["c"] = 3
        return d


    def main() -> None:
        d = build()
        print(len(dict(d)))
        print("a" in dict(d))
        print(any(d))
        print(all(d))
        zipped: int = 0
        for a, b in zip(d, d):
            zipped = zipped + 1
        print(zipped)
        print(len(list(zip(d, d))))


    main()
"""


def test_mapping_builtin_family_matches_cpython(tmp_path):
    """dict(), any(), all() and zip() over a mapping iterate its keys."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(_FAMILY).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert native.returncode == 0, native.stderr
    # Every one of these was wrong before the sweep: 0, False, False, False,
    # and the two zips raised KeyError.
    assert native.stdout.split() == [
        "3", "True", "True", "True", "3", "3",
    ], native.stdout


# ---------------------------------------------------------------------------
# Ownership.  py_dict_keys / py_list_get / py_dict_get all return NEW refs
# (pcc/runtime/include/py_runtime.h), and py_dict_set / py_set_add retain
# what they store rather than stealing it.  The mapping normalisation added
# for set/dict/any/zip therefore creates owned temporaries, and the first cut
# of that sweep leaked every one of them.  Output-only tests cannot see a
# leak, so assert the release is emitted.
# ---------------------------------------------------------------------------


def _release_balance(ir_text: str, body: str) -> tuple[int, int]:
    return body.count("@py_dict_keys"), body.count("@pcc_gc_release")


@pytest.mark.parametrize(
    "expression",
    [
        "frozenset(d)",
        "set(d)",
        "dict(d)",
        "any(d)",
        "all(d)",
        "list(zip(d, d))",
    ],
)
def test_mapping_normalisation_releases_its_keys_list(expression):
    """Every materialised keys list must be released, not leaked."""
    ir_text = _compile_to_ll(
        f"""
        def f(d: dict) -> object:
            return {expression}
        """,
        "mapping_release_" + re.sub(r"\W+", "_", expression),
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    keys_calls, releases = _release_balance(ir_text, body)
    assert keys_calls >= 1, f"{expression} did not normalise through py_dict_keys:\n{body}"
    assert releases >= keys_calls, (
        f"{expression}: {keys_calls} py_dict_keys call(s) but only {releases} "
        f"pcc_gc_release call(s) — an owned keys list is leaking:\n{body}"
    )


_OWNED_PRODUCERS = (
    "@py_dict_keys",
    "@py_list_get",
    "@py_dict_get",
    "@py_obj_getitem",
    "@py_int_from_i64",
    "@py_tuple_new",
)


@pytest.mark.parametrize(
    "expression",
    ["any(d)", "all(d)", "dict(d)", "list(zip(d, d))"],
)
def test_every_owned_producer_in_the_loop_has_a_release(expression):
    """Count releases against owned-ref producers, not just the keys list.

    ``py_dict_keys``/``py_list_get``/``py_dict_get``/``py_obj_getitem``/
    ``py_int_from_i64``/``py_tuple_new`` all hand back a NEW reference, and
    the containers they feed (``py_dict_set``, ``py_tuple_set_item``,
    ``py_list_append``, ``py_set_add``) RETAIN rather than steal — verified in
    the runtime: ``py_tuple_set_item`` increfs on the GC0 path and uses the
    balanced ``pcc_gc_store_ptr`` otherwise.  So each of these needs a
    matching release, and the first cut of the mapping sweep released only
    the keys list.
    """
    ir_text = _compile_to_ll(
        f"""
        def f(d: dict) -> object:
            return {expression}
        """,
        "owned_release_" + re.sub(r"\W+", "_", expression),
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    produced = sum(body.count(name) for name in _OWNED_PRODUCERS)
    released = body.count("@pcc_gc_release")
    assert produced >= 1, f"{expression} produced no owned refs:\n{body}"
    assert released >= produced, (
        f"{expression}: {produced} owned-ref producer call(s) but only "
        f"{released} pcc_gc_release call(s) — at least one reference is "
        f"leaked every iteration:\n{body}"
    )


def test_dict_copy_releases_each_entry_key_and_value():
    """dict(mapping) must not leak one key + one value per entry."""
    ir_text = _compile_to_ll(
        """
        def f(d: dict) -> object:
            return dict(d)
        """,
        "mapping_release_dict_entries",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@py_list_get" in body, body
    assert "@py_dict_get" in body, body
    # keys list + per-entry key + per-entry value.
    assert body.count("@pcc_gc_release") >= 3, body


_STRICT_ZIP = """
    def build() -> dict:
        d: dict = {}
        d["a"] = 1
        d["b"] = 2
        return d


    def main() -> None:
        d = build()
        n: int = 0
        for a, b in zip(d, d, strict=True):
            n = n + 1
        print(n)
        print(len(list(zip(d, d, strict=True))))


    main()
"""


def test_strict_zip_over_a_mapping_still_lowers(tmp_path):
    """zip(mapping, ..., strict=True) must reach a lowering, not fall off both.

    The for-loop rewrite accepts `strict` and drops it, and now declines
    mapping sources; the generic zip builtin used to reject every kwarg.  The
    combination reached neither and died at runtime with
    `NameError: name 'zip' is not defined`.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(_STRICT_ZIP).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["2", "2"], native.stdout
