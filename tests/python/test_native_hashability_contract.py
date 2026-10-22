"""Native mutable-container hashability and error propagation.

Mutable builtin containers are never valid hash keys.  The runtime hash API
uses the CPython convention: ``-1`` plus a pending ``TypeError`` signals a
hash failure, while a legitimate hash value of ``-1`` is normalized to
``-2``.  Dict/set callers must inspect the pending exception before lookup or
mutation so APIs such as ``dict.get`` do not turn an unhashable key into a
default value or ``KeyError``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_hash_runtime_mirrors_reject_mutable_builtins_and_guard_callers():
    compare_py = (
        REPO / "pcc" / "runtime" / "py" / "py_obj_ops_compare.py"
    ).read_text(encoding="utf-8")
    dict_py = (REPO / "pcc" / "runtime" / "py" / "py_dict.py").read_text(
        encoding="utf-8"
    )
    set_py = (REPO / "pcc" / "runtime" / "py" / "py_set.py").read_text(
        encoding="utf-8"
    )

    for source in (compare_py,):
        for type_name in ("list", "dict", "set", "bytearray"):
            assert f"unhashable type: '{type_name}'" in source
        assert "PY_TYPE_LIST" in source
        assert "PY_TYPE_DICT" in source
        assert "PY_TYPE_SET" in source
        assert "PY_TYPE_BYTEARRAY" in source
        assert "py_err_occurred()" in source

    # Every hash computed for a probe or mutation is followed by the pending
    # exception check before the table is touched. The GC0 fast path hashes
    # only tagged ints and str, which cannot fail.
    fast0 = "def _dict_fast0_hash(" + dict_py.split("def _dict_fast0_hash(", 1)[1].split("\n\n\n", 1)[0]
    assert "py_str_hash(key)" in fast0 and "kind == 1" in fast0
    for source, expected in ((dict_py.replace(fast0, ""), 2), (set_py, 3)):
        sites = [match.start() for match in re.finditer(r"py_obj_hash\(", source)]
        assert len(sites) == expected
        for site in sites:
            following = "\n".join(source[site:].splitlines()[:6])
            assert "py_err_occurred()" in following, following

    assert dict_py.count(
        "if ptr_is_null(v) == 0:\n        return v\n"
        "    if py_err_occurred() != 0:\n        return null()"
    ) == 2
    assert (
        "if ptr_is_null(v) == 0:\n        py_dict_del(d, key)\n        return v\n"
        "    if py_err_occurred() != 0:\n        return null()"
        in dict_py
    )


def test_mutable_builtin_hash_failures_match_cpython_without_mutation(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent(
        """
        def main() -> None:
            try:
                hash([])
                print("hash-list-missed")
            except TypeError:
                print("hash-list")
            try:
                hash({})
                print("hash-dict-missed")
            except TypeError:
                print("hash-dict")
            try:
                hash(set())
                print("hash-set-missed")
            except TypeError:
                print("hash-set")
            try:
                hash(bytearray(b"x"))
                print("hash-bytearray-missed")
            except TypeError:
                print("hash-bytearray")
            try:
                hash(([],))
                print("hash-nested-tuple-missed")
            except TypeError:
                print("hash-nested-tuple")

            values = {"ok": 1}
            try:
                values[[]] = 2
                print("dict-set-missed")
            except TypeError:
                print("dict-set")
            try:
                values.get([], 9)
                print("dict-get-missed")
            except TypeError:
                print("dict-get")
            try:
                values[[]]
                print("dict-getitem-missed")
            except TypeError:
                print("dict-getitem")
            try:
                del values[[]]
                print("dict-delitem-missed")
            except TypeError:
                print("dict-delitem")
            try:
                values.pop([])
                print("dict-pop-missed")
            except TypeError:
                print("dict-pop")
            try:
                values.pop([], 9)
                print("dict-pop-default-missed")
            except TypeError:
                print("dict-pop-default")
            try:
                values.setdefault([], 9)
                print("dict-setdefault-missed")
            except TypeError:
                print("dict-setdefault")
            try:
                [] in values
                print("dict-contains-missed")
            except TypeError:
                print("dict-contains")
            print(len(values), values["ok"])

            members = {1}
            try:
                members.add([])
                print("set-add-missed")
            except TypeError:
                print("set-add")
            try:
                [] in members
                print("set-contains-missed")
            except TypeError:
                print("set-contains")
            try:
                members.remove([])
                print("set-remove-missed")
            except TypeError:
                print("set-remove")
            try:
                members.discard([])
                print("set-discard-missed")
            except TypeError:
                print("set-discard")
            try:
                set([[]])
                print("set-constructor-missed")
            except TypeError:
                print("set-constructor")
            try:
                members.update([[]])
                print("set-update-missed")
            except TypeError:
                print("set-update")
            print(len(members), 1 in members)

            try:
                {item for item in [[]]}
                print("set-comprehension-missed")
            except TypeError:
                print("set-comprehension")
            try:
                {item: 1 for item in [[]]}
                print("dict-comprehension-missed")
            except TypeError:
                print("dict-comprehension")

            try:
                dict.fromkeys([[1]])
                print("fromkeys-missed")
            except TypeError:
                print("fromkeys")

        main()
        """
    ).lstrip()
    expected = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout

    src = tmp_path / "hashability.py"
    exe = tmp_path / "hashability.out"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
