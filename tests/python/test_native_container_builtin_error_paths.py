"""Container-builtin walks must fail closed on their exception edges.

Positional walks (``dict(d)`` copy, ``zip()``, and formerly ``any()``/
``all()``) acquire owned temporaries — a ``py_dict_keys`` view, per-iteration
element/index refs, the result under construction — and then call runtime
helpers that can raise for DynType sources.  Two defect classes, found by
external review plus reading the runtime contracts:

1. dict-copy's existing ``py_err_occurred`` checks jumped to the error exit
   WITHOUT releasing the owned keys view / loop temps: a leak on every
   raising edge.
2. any/all and zip had NO checks inside their walks at all.  A mid-loop
   raise (``py_obj_getitem`` KeyError on a dyn-typed dict) left the error
   in TLS, kept looping over NULL elements, and returned a silently wrong
   result.  Worse, ``py_dict_keys`` on a non-dict returns NULL WITHOUT
   raising (py_dict.c), so ``dict(x)`` over a dyn non-dict silently
   produced ``{}`` where CPython raises TypeError.

CPython-truth note: ``any({"a": 1})`` is ``True`` in CPython (iterating a
mapping yields keys).  ``any()``/``all()`` now run CPython's iterator
protocol in the runtime (``py_builtin_any_all``), so a dyn-held mapping
iterates its keys exactly as CPython does.  For ``zip()`` over a dyn-held
mapping the contract pinned here is still FAIL CLOSED -- a clean catchable
exception, never a silent wrong answer.  The dyn non-dict ``dict(x)`` case
IS CPython-correct here (TypeError).

any/all make one runtime call with one error check, whatever the static
shape: an item's ``__bool__`` can raise even for a proven built-in source.
Zip uses registered owner transactions: even static shapes check
lease/publication errors and clean up through root slots.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from tests.native_provisioning import require_native_provisioning_allowed
from tests.runtime_fixture_provenance import _verified_test_runtime_archive

_REPO_ROOT = Path(__file__).absolute().parents[2]


def _compile_to_ll(source: str, name: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    with tempfile.TemporaryDirectory(prefix="pcc-container-ir-") as temporary:
        src = Path(temporary) / f"{name}.py"
        out = Path(temporary) / f"{name}.ll"
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


def _run_native(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE")
    runtime_options = {}
    if explicit:
        archive, _manifest = _verified_test_runtime_archive(explicit)
        runtime_options["runtime_archive"] = str(archive)
    else:
        require_native_provisioning_allowed(_REPO_ROOT)

    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
        **runtime_options,
    )
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60
    )


# ---------------------------------------------------------------- semantic


def test_dict_of_non_dict_dyn_raises_typeerror(tmp_path):
    """CPython-correct: ``dict(5)`` is a TypeError.  The silent path was
    py_dict_keys(non-dict) -> NULL without raising -> empty dict."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                d2 = dict(x)
                print("returned")
                print(len(d2))
            except Exception:
                print("caught")

        def main() -> None:
            check(5)

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout


def test_any_over_dyn_mapping_iterates_keys(tmp_path):
    """CPython: iterating a mapping yields its keys, so any({"a": 1}) is
    True. The positional walk raised KeyError mid-walk instead."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                r = any(x)
                print("returned")
                print(r)
            except Exception:
                print("caught")

        def main() -> None:
            check({"a": 1})

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["returned", "True"], native.stdout


def test_zip_over_dyn_mapping_fails_closed(tmp_path):
    """Interim fail-closed contract: the dyn arg's mid-walk KeyError must
    not produce a result list holding NULL-slotted tuples."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                pairs = list(zip(("q",), x))
                print("returned")
                print(len(pairs))
            except Exception:
                print("caught")

        def main() -> None:
            check({"a": 1})

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout


def test_dict_of_dyn_mapping_copies_like_cpython(tmp_path):
    """dict(x) where x dynamically holds a dict is a shallow copy in
    CPython.  The old pairs walk indexed the mapping positionally and
    silently corrupted; the tag dispatch routes it to py_dict_update."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            d2 = dict(x)
            print(len(d2))
            print(d2["a"])
            print(d2["b"])

        def main() -> None:
            check({"a": 1, "b": 2})

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["2", "1", "2"], native.stdout


def test_dict_of_dyn_pair_list_still_builds(tmp_path):
    """The pairs walk must keep working for a dyn-held list of pairs."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            d2 = dict(x)
            print(len(d2))
            print(d2["b"])

        def main() -> None:
            check([("a", 1), ("b", 2)])

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["2", "2"], native.stdout


def test_dict_of_dyn_set_fails_closed(tmp_path):
    """CPython accepts any iterable of pairs; the pcc pairs walk is
    positional and cannot index a set, so a dyn-held set fails closed
    with TypeError instead of silently returning an empty dict."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                d2 = dict(x)
                print("returned")
                print(len(d2))
            except Exception:
                print("caught")

        def main() -> None:
            check({1, 2})

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout


def test_dict_of_dyn_non_pair_element_fails_closed(tmp_path):
    """py_obj_getitem returns NULL without raising for a non-subscriptable
    pair element; the walk previously fed NULL key/value into py_dict_set."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                d2 = dict(x)
                print("returned")
                print(len(d2))
            except Exception:
                print("caught")

        def main() -> None:
            check([("a", 1), 3])

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout


def test_any_over_dyn_scalar_raises_typeerror(tmp_path):
    """CPython: any(5) is TypeError('int' object is not iterable).  The
    silent path was py_obj_len(int) -> 0 -> loop never entered -> False."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                r = any(x)
                print("returned")
                print(r)
            except Exception:
                print("caught")

        def main() -> None:
            check(5)

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout


def test_zip_over_dyn_scalar_raises_typeerror(tmp_path):
    """CPython: zip(t, 5) is TypeError.  The silent path was
    py_obj_len(int) -> 0 -> min_len 0 -> empty result."""
    native = _run_native(
        tmp_path,
        """
        def check(x: object) -> None:
            try:
                pairs = list(zip((1, 2), x))
                print("returned")
                print(len(pairs))
            except Exception:
                print("caught")

        def main() -> None:
            check(5)

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout


def test_zip_strict_unequal_raises_valueerror(tmp_path):
    """CPython: zip(strict=True) raises ValueError on unequal lengths.
    Both pcc paths silently truncated to the shortest input."""
    native = _run_native(
        tmp_path,
        """
        def main() -> None:
            try:
                pairs = list(zip((1, 2), (3,), strict=True))
                print("returned")
                print(len(pairs))
            except Exception as exc:
                print("caught")
                print(str(exc))

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    lines = native.stdout.split("\n")
    assert lines[0] == "caught", native.stdout
    assert "different lengths" in native.stdout, native.stdout


def test_for_zip_strict_unequal_raises_valueerror(tmp_path):
    """The for-loop zip rewrite dropped strict entirely; it must decline
    and route to the builtin, which enforces it."""
    native = _run_native(
        tmp_path,
        """
        def main() -> None:
            total = 0
            try:
                for a, b in zip((1, 2), (3,), strict=True):
                    total = total + a + b
                print("returned")
                print(total)
            except Exception as exc:
                print("caught")
                print(str(exc))

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split("\n")[0] == "caught", native.stdout


def test_zip_strict_equal_lengths_builds_all_pairs(tmp_path):
    native = _run_native(
        tmp_path,
        """
        def main() -> None:
            total = 0
            for a, b in zip((1, 2), (30, 40), strict=True):
                total = total + a + b
            print(total)

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["73"], native.stdout


def test_zip_without_strict_still_truncates(tmp_path):
    native = _run_native(
        tmp_path,
        """
        def main() -> None:
            pairs = list(zip((1, 2), (3,)))
            print(len(pairs))

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["1"], native.stdout


# ---------------------------------------------------------------- IR shape


def test_dict_pairs_walk_releases_loop_temps():
    """The pairs walk owns six NEW refs per iteration (pair, key, value,
    two index boxes, the loop index box); py_dict_set retains, so all six
    must be released on the normal path."""
    ir_text = _compile_to_ll(
        """
        def f(pairs: list) -> dict:
            return dict(pairs)
        """,
        "dict_pairs_walk_releases",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    assert body.count("@pcc_gc_release") >= 6, body.count("@pcc_gc_release")


def test_dict_copy_error_edges_release_owned_temps():
    """Both dict-copy error edges retire their owners and restore original TLS."""
    from tests.python.test_slot_call_exception_cleanup import assert_dict_copy_error_cleanup

    ir_text = _compile_to_ll(
        """
        def f(d: dict) -> dict:
            return dict(d)
        """,
        "dict_copy_err_cleanup",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    assert_dict_copy_error_cleanup(body)


def test_zip_dyn_source_edges_check_and_release():
    """zip(static_dict, dyn): the dyn arg's len and getitem calls get
    fail-closed checks whose cleanup releases the owned keys view."""
    ir_text = _compile_to_ll(
        """
        def f(d: dict, x: object) -> object:
            return zip(d, x)
        """,
        "zip_dyn_err_cleanup",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    assert body.count("@py_err_occurred") >= 2, body
    assert "call.slot.cleanup" in body, body
    # Cleanup clears authoritative owners before finalizers or frame leave.
    # A raw pcc_gc_release(value) would reintroduce the stale-address gap.
    assert "@pcc_gc_store_root" in body, body
    assert "@pcc_gc_frame_leave_lifo" in body, body
    assert "zip.arg.0.keys.operand" in body, body
    assert "zip.row.operand" in body and "zip.index.operand" in body, body


def test_any_over_static_dict_is_one_runtime_iteration():
    """Cost guard: any(d) is one runtime iterator-protocol call; no inline
    positional walk (len/getitem per element) is emitted for any shape."""
    ir_text = _compile_to_ll(
        """
        def f(d: dict) -> bool:
            return any(d)
        """,
        "any_static_runtime",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    assert body.count("@py_builtin_any_all(") == 1, body
    assert "@py_obj_len(" not in body and "@py_obj_getitem(" not in body, body


def test_any_over_dyn_source_checks_the_runtime_status():
    """any(dyn) checks the runtime's error after its single iteration call."""
    ir_text = _compile_to_ll(
        """
        def f(x: object) -> bool:
            return any(x)
        """,
        "any_dyn_checks",
    )
    body = _function_body(ir_text, "f")
    assert body is not None
    call = body.index("@py_builtin_any_all(")
    assert "@py_err_occurred" in body[call:], body
