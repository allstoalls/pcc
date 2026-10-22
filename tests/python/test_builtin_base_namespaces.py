"""BaseException, KeyError and dict answer from native namespaces, as CPython's do."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run_on_five_collectors(tmp_path, stem, source, expected):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / (stem + ".py")
    exe = tmp_path / (stem + ".out")
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        run_env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend} rc={result.returncode}\n"
            + result.stdout + result.stderr
        )
        assert result.stdout == expected, f"PCC_GC_BACKEND={backend}"


def _cpython_output(source):
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout


def test_super_through_builtin_bases_matches_cpython(tmp_path, monkeypatch, pcc_runtime_archive):
    # super() reaches dict's members on the subclass's item storage
    # (__missing__ still answers __getitem__), BaseException's __str__,
    # __repr__, __init__ and add_note, and KeyError's own __str__.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class Lower(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key.lower(), value)

            def __getitem__(self, key):
                return super().__getitem__(key.lower())

            def __delitem__(self, key):
                super().__delitem__(key.lower())

            def __contains__(self, key):
                return super().__contains__(key.lower())

            def __repr__(self):
                return "Lower(" + super().__repr__() + ")"

            def update(self, other):
                for key in other:
                    self[key] = other[key]

            def size(self):
                return super().__len__()

            def keys_via(self):
                return sorted(super().keys())

            def pop_via(self, key):
                return super().pop(key)

            def get_via(self, key, default):
                return super().get(key, default)


        class Missing(dict):
            def __missing__(self, key):
                return "missing:" + key

            def fetch(self, key):
                return super().__getitem__(key)


        class MyErr(ValueError):
            def __str__(self):
                return "x:" + super().__str__()

            def __repr__(self):
                return "R" + super().__repr__()


        class MyKey(KeyError):
            def __str__(self):
                return "k:" + super().__str__()


        class Tagged(Exception):
            def __init__(self, code, text):
                super().__init__(text)
                self.code = code

            def __str__(self):
                return str(self.code) + "/" + super().__str__()


        class Noted(Exception):
            def noted(self, text):
                super().add_note(text)
                return self.__notes__


        def show(label, thunk):
            try:
                print(label, repr(thunk()))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            d = Lower()
            show("d-set", lambda: d.__setitem__("A", 1))
            d["B"] = 2
            show("d-get", lambda: d["a"])
            show("d-in", lambda: ("A" in d, "z" in d))
            show("d-repr", lambda: repr(d))
            show("d-size", lambda: d.size())
            show("d-keys", lambda: d.keys_via())
            show("d-update", lambda: (d.update({"C": 3}), d["c"]))
            show("d-del", lambda: (d.__delitem__("A"), "a" in d))
            show("d-pop", lambda: d.pop_via("b"))
            show("d-get-via", lambda: (d.get_via("c", 0), d.get_via("q", 0)))
            show("d-pop-missing", lambda: d.pop_via("q"))
            m = Missing()
            m["x"] = "y"
            show("m-get", lambda: (m.fetch("x"), m.fetch("q")))
            show("e-str", lambda: str(MyErr("boom")))
            show("e-repr", lambda: repr(MyErr("boom")))
            show("e-str2", lambda: str(MyErr("a", 2)))
            show("e-str0", lambda: str(MyErr()))
            show("k-str", lambda: str(MyKey("kk")))
            show("t-str", lambda: str(Tagged(7, "bad")))
            show("noted", lambda: Noted().noted("hint"))


        main()
        """
    )
    expected = (
        'd-set None\n'
        'd-get 1\n'
        'd-in (True, False)\n'
        'd-repr "Lower({\'a\': 1, \'b\': 2})"\n'
        'd-size 2\n'
        "d-keys ['a', 'b']\n"
        'd-update (None, 3)\n'
        'd-del (None, False)\n'
        'd-pop 2\n'
        'd-get-via (3, 0)\n'
        "d-pop-missing KeyError 'q'\n"
        "m-get ('y', 'missing:q')\n"
        "e-str 'x:boom'\n"
        'e-repr "RMyErr(\'boom\')"\n'
        'e-str2 "x:(\'a\', 2)"\n'
        "e-str0 'x:'\n"
        'k-str "k:\'kk\'"\n'
        "t-str '7/bad'\n"
        "noted ['hint']\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "super_builtin_bases", source, expected)


def test_builtin_base_members_by_class_and_instance_match_cpython(tmp_path, monkeypatch, pcc_runtime_archive):
    # The same members through the class (dict.__setitem__(d, k, v),
    # Exception.__str__(e), KeyError.__str__), through a dict subclass or
    # exception instance (d.update(pairs, **kw), d.copy(), e.add_note()), on
    # an exact dict, and on a builtin exception object, with CPython's
    # argument, receiver and KeyError messages.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class D(dict):
            pass


        class E(ValueError):
            pass


        class K(KeyError):
            pass


        def show(label, thunk):
            try:
                print(label, repr(thunk()))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            d = D()
            d["a"] = 1
            show("d-update", lambda: (d.update({"b": 2}), sorted(d.keys())))
            show("d-update-pairs", lambda: (d.update([("c", 3)], z=26), sorted(d.items())))
            show("d-copy", lambda: (type(d.copy()).__name__, sorted(d.copy().items())))
            show("d-popitem", lambda: d.popitem())
            show("d-unbound-set", lambda: (dict.__setitem__(d, "y", 9), d["y"]))
            show("d-unbound-get", lambda: (dict.__getitem__(d, "y"), dict.get(d, "nope", 0)))
            show("d-method", lambda: (d.__setitem__("w", 1), d.__contains__("w"), d.__len__()))
            show("d-pop-missing", lambda: d.pop("missing"))
            show("d-getitem-missing", lambda: d["missing"])
            show("d-or", lambda: sorted(dict.__or__(d, {"q": 0}).items()))
            show("d-eq", lambda: (dict.__eq__(d, dict(d)), dict.__eq__(d, 1)))
            show("d-clear", lambda: (d.clear(), len(d)))
            show("d-arity", lambda: dict.get(d))
            show("d-wrong", lambda: dict.__setitem__(1, 2, 3))
            plain = {"p": 1}
            show("exact-unbound", lambda: (dict.__setitem__(plain, "q", 2), sorted(plain.items())))
            e = E("boom")
            show("e-unbound-str", lambda: Exception.__str__(e))
            show("e-unbound-repr", lambda: BaseException.__repr__(e))
            show("e-note", lambda: (e.add_note("n"), e.add_note("m"), e.__notes__))
            show("e-note-type", lambda: e.add_note(1))
            show("k-str", lambda: (KeyError.__str__(K("kk")), BaseException.__str__(K("kk"))))
            show("e-init", lambda: (BaseException.__init__(e, 1, 2), e.args))
            show("e-init-kw", lambda: BaseException.__init__(e, a=1))
            show("e-wrong", lambda: BaseException.__str__(1))
            try:
                raise ValueError("builtin")
            except ValueError as exc:
                show("builtin-str", lambda: exc.__str__())
                show("builtin-repr", lambda: exc.__repr__())


        main()
        """
    )
    expected = (
        "d-update (None, ['a', 'b'])\n"
        "d-update-pairs (None, [('a', 1), ('b', 2), ('c', 3), ('z', 26)])\n"
        "d-copy ('dict', [('a', 1), ('b', 2), ('c', 3), ('z', 26)])\n"
        "d-popitem ('z', 26)\n"
        'd-unbound-set (None, 9)\n'
        'd-unbound-get (9, 0)\n'
        'd-method (None, True, 5)\n'
        "d-pop-missing KeyError 'missing'\n"
        "d-getitem-missing KeyError 'missing'\n"
        "d-or [('a', 1), ('b', 2), ('c', 3), ('q', 0), ('w', 1), ('y', 9)]\n"
        'd-eq (True, NotImplemented)\n'
        'd-clear (None, 0)\n'
        'd-arity TypeError get expected at least 1 argument, got 0\n'
        "d-wrong TypeError descriptor '__setitem__' requires a 'dict' object but received a 'int'\n"
        "exact-unbound (None, [('p', 1), ('q', 2)])\n"
        "e-unbound-str 'boom'\n"
        'e-unbound-repr "E(\'boom\')"\n'
        "e-note (None, None, ['n', 'm'])\n"
        'e-note-type TypeError add_note() argument must be str, not int\n'
        'k-str ("\'kk\'", \'kk\')\n'
        'e-init (None, (1, 2))\n'
        'e-init-kw TypeError E() takes no keyword arguments\n'
        "e-wrong TypeError descriptor '__str__' requires a 'BaseException' object but received a 'int'\n"
        "builtin-str 'builtin'\n"
        'builtin-repr "ValueError(\'builtin\')"\n'
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "builtin_base_members", source, expected)


def test_codegen_member_tables_match_the_runtime_namespaces():
    # A call through a builtin type name (dict.get, Exception.__str__) takes
    # the native type object only for members its namespace provides; the
    # codegen tables and the runtime namespaces must not drift apart.
    import re

    from pcc.frontends.python.codegen.class_gen import ClassLowering
    from pcc.frontends.python.codegen.unary_call_lowering import (
        NATIVE_DICT_NAMESPACE_MEMBERS,
        NATIVE_EXCEPTION_NAMESPACE_MEMBERS,
    )
    from tests.python import pcc1_gate

    source = (pcc1_gate.repo_root() / "pcc/runtime/py/py_class.py").read_text(encoding="utf-8")
    body = source.split("def _builtin_namespace_names(index: int):", 1)[1].split("\ndef ", 1)[0]
    tables = re.findall(r"cstr\('([^']*)'\)", body)
    assert len(tables) == 3
    assert set(tables[0].split()) == NATIVE_EXCEPTION_NAMESPACE_MEMBERS
    assert set(tables[2].split()) == NATIVE_DICT_NAMESPACE_MEMBERS
    ordinary = {name for name in NATIVE_DICT_NAMESPACE_MEMBERS if not name.startswith("__")}
    assert set(ClassLowering._DICT_SUBCLASS_RUNTIME_METHODS) == ordinary


def test_builtin_base_members_survive_collection_pressure(tmp_path, monkeypatch, pcc_runtime_archive):
    # The dict and exception members hold storage, keys and notes across
    # allocating calls; a low debt threshold collects (and, on the relocating
    # collectors, moves objects) between them.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    monkeypatch.setenv("PCC_GC_DEBT_THRESHOLD", "1024")

    source = textwrap.dedent(
        """
        class Lower(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key.lower(), value)

            def __getitem__(self, key):
                return super().__getitem__(key.lower())


        class Noted(ValueError):
            def __str__(self):
                return "n:" + super().__str__()


        def main():
            ok = 0
            for i in range(600):
                d = Lower()
                d["K" + str(i)] = [i, str(i)]
                dict.update(d, [("x" + str(i), (i,))], extra=str(i))
                if d["k" + str(i)][0] == i and dict.get(d, "x" + str(i)) == (i,):
                    ok += 1
                copied = dict.copy(d)
                if len(copied) == 3 and dict.pop(d, "extra") == str(i) and len(d) == 2:
                    ok += 1
                if repr(dict.__or__(d, {"z": i})).startswith("{"):
                    ok += 1
                e = Noted("v" + str(i))
                e.add_note("a" + str(i))
                e.add_note("b" + str(i))
                if str(e) == "n:v" + str(i) and e.__notes__ == ["a" + str(i), "b" + str(i)]:
                    ok += 1
            print(ok)


        main()
        """
    )
    expected = "2400\n"
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "builtin_members_pressure", source, expected)
