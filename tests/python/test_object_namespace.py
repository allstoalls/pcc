"""``object``'s namespace and ``super()`` through it follow CPython natively."""

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


def test_object_members_match_cpython(tmp_path, monkeypatch, pcc_runtime_archive):
    # Every class reaches object's members (comparisons, hash, init, repr,
    # str, format, init_subclass, subclasshook, getattribute, getstate,
    # setattr, delattr) as real callables with CPython's argument checks
    # and messages, before __getattr__ and behind any user override. An
    # instance dict that answers one of these names does not hide object's
    # member from the class's other instances.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class A:
            pass


        class Custom:
            def __init__(self, n):
                super().__init__()
                self.n = n

            def __repr__(self):
                return "Custom(" + str(self.n) + ")"

            def __eq__(self, other):
                return isinstance(other, Custom) and other.n == self.n

            def __hash__(self):
                return super().__hash__() * 0 + self.n


        class Hooked:
            def __setattr__(self, name, value):
                super().__setattr__(name, "set:" + str(value))

            def __getattribute__(self, name):
                if name == "secret":
                    return "intercepted"
                return super().__getattribute__(name)


        def default_repr(text):
            return text.startswith("<") and " object at 0x" in text and text.endswith(">")


        def show(label, thunk):
            try:
                value = thunk()
                if value is NotImplemented:
                    print(label, "<NotImplemented>")
                else:
                    print(label, repr(value))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            a = A()
            show("eq-self", lambda: a.__eq__(a))
            show("eq-other", lambda: a.__eq__(1))
            show("ne-self", lambda: a.__ne__(a))
            show("ne-other", lambda: a.__ne__(1))
            show("lt", lambda: a.__lt__(a))
            show("le", lambda: a.__le__(a))
            show("gt", lambda: a.__gt__(a))
            show("ge", lambda: a.__ge__(a))
            show("hash", lambda: a.__hash__() == hash(a))
            show("init", lambda: a.__init__())
            show("init-args", lambda: a.__init__(1))
            show("class-init", lambda: A.__init__(a))
            show("repr", lambda: default_repr(a.__repr__()))
            show("str", lambda: default_repr(a.__str__()))
            show("format", lambda: default_repr(a.__format__("")))
            show("format-spec", lambda: a.__format__("x"))
            show("format-type", lambda: a.__format__(1))
            show("init-subclass", lambda: A.__init_subclass__())
            show("init-subclass-kw", lambda: A.__init_subclass__(x=1))
            show("init-subclass-pos", lambda: A.__init_subclass__(1))
            show("subclasshook", lambda: A.__subclasshook__(int))
            show("getattribute-missing", lambda: a.__getattribute__("zz"))
            show("getattribute-type", lambda: a.__getattribute__(1))
            show("getstate-empty", lambda: a.__getstate__())
            a.x = 5
            show("getstate", lambda: a.__getstate__())
            show("setattr", lambda: (a.__setattr__("y", 6), a.y))
            show("delattr", lambda: (a.__delattr__("y"), hasattr(a, "y")))
            show("delattr-missing", lambda: a.__delattr__("zz"))
            show("eq-arity", lambda: a.__eq__())
            show("hash-arity", lambda: a.__hash__(1))
            show("hash-kw", lambda: a.__hash__(k=1))
            show("format-arity", lambda: a.__format__())
            show("setattr-arity", lambda: a.__setattr__("x"))
            show("sizeof", lambda: type(a.__sizeof__()).__name__)
            show("doc", lambda: (A.__doc__, a.__doc__))
            shadow = A()
            shadow.__eq__ = "shadow"
            # The dict hit must not record that A answers nothing for __eq__.
            show("shadow", lambda: (shadow.__eq__, A().__eq__(1) is NotImplemented))
            c = Custom(3)
            show("custom", lambda: (repr(c), c == Custom(3), hash(c), c.__ne__(Custom(3)), c.__str__()))
            show("hasattr", lambda: (hasattr(a, "__init__"), hasattr(a, "__eq__"), hasattr(a, "nope")))
            h = Hooked()
            h.__setattr__("value", 4)
            show("hooked", lambda: (h.value, h.secret))


        main()
        """
    )
    expected = (
        'eq-self True\n'
        'eq-other <NotImplemented>\n'
        'ne-self False\n'
        'ne-other <NotImplemented>\n'
        'lt <NotImplemented>\n'
        'le <NotImplemented>\n'
        'gt <NotImplemented>\n'
        'ge <NotImplemented>\n'
        'hash True\n'
        'init None\n'
        'init-args TypeError A.__init__() takes exactly one argument (the instance to initialize)\n'
        'class-init None\n'
        'repr True\n'
        'str True\n'
        'format True\n'
        'format-spec TypeError unsupported format string passed to A.__format__\n'
        'format-type TypeError __format__() argument must be str, not int\n'
        'init-subclass None\n'
        'init-subclass-kw TypeError A.__init_subclass__() takes no keyword arguments\n'
        'init-subclass-pos TypeError A.__init_subclass__() takes no arguments (1 given)\n'
        'subclasshook <NotImplemented>\n'
        "getattribute-missing AttributeError 'A' object has no attribute 'zz'\n"
        "getattribute-type TypeError attribute name must be string, not 'int'\n"
        'getstate-empty None\n'
        "getstate {'x': 5}\n"
        'setattr (None, 6)\n'
        'delattr (None, False)\n'
        "delattr-missing AttributeError 'A' object has no attribute 'zz'\n"
        'eq-arity TypeError expected 1 argument, got 0\n'
        'hash-arity TypeError expected 0 arguments, got 1\n'
        'hash-kw TypeError wrapper __hash__() takes no keyword arguments\n'
        'format-arity TypeError object.__format__() takes exactly one argument (0 given)\n'
        'setattr-arity TypeError __setattr__ expected 2 arguments, got 1\n'
        "sizeof 'int'\n"
        'doc (None, None)\n'
        "shadow ('shadow', True)\n"
        "custom ('Custom(3)', True, 3, False, 'Custom(3)')\n"
        'hasattr (True, True, False)\n'
        "hooked ('set:4', 'intercepted')\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "object_members", source, expected)


def test_super_through_object_matches_cpython(tmp_path, monkeypatch, pcc_runtime_archive):
    # super() past the last user class reaches object's namespace: repr,
    # eq, hash, setattr, init (with CPython's excess-argument rule) and
    # format bind to the receiver, including in a function-local class
    # whose base is only known at run time. A dict subclass reaches dict's
    # own methods through super().
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class P:
            def __init__(self, n):
                super().__init__()
                self.n = n

            def __repr__(self):
                text = super().__repr__()
                return "P" + str(text.startswith("<") and text.endswith(">"))

            def __eq__(self, other):
                return super().__eq__(other)

            def __hash__(self):
                return super().__hash__() * 0 + self.n

            def __setattr__(self, name, value):
                super().__setattr__(name, value)

            def reinit(self, *args):
                return super().__init__(*args)

            def fmt(self, spec):
                return super().__format__(spec)


        class Base:
            pass


        def make(base):
            class Local(base):
                def __init__(self, n):
                    super().__init__()
                    self.n = n

                def text(self):
                    return super().__str__().startswith("<")

            return Local


        class Getter(dict):
            def fetch(self, key):
                return super().get(key)


        def show(label, thunk):
            try:
                print(label, repr(thunk()))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            p = P(1)
            show("repr", lambda: repr(p))
            show("eq", lambda: (p == p, p.__eq__(1) is NotImplemented))
            show("hash", lambda: hash(p))
            show("attr", lambda: p.n)
            show("reinit", lambda: p.reinit())
            show("reinit-args", lambda: p.reinit(1))
            show("fmt", lambda: p.fmt("")[:1])
            show("fmt-spec", lambda: p.fmt("x"))
            L = make(Base)
            show("local", lambda: (L(3).n, L(3).text()))
            d = Getter()
            d["a"] = "abc"
            show("dict-get", lambda: d.fetch("a"))


        main()
        """
    )
    expected = (
        "repr 'PTrue'\n"
        'eq (True, True)\n'
        'hash 1\n'
        'attr 1\n'
        'reinit None\n'
        'reinit-args TypeError object.__init__() takes exactly one argument (the instance to initialize)\n'
        "fmt 'P'\n"
        'fmt-spec TypeError unsupported format string passed to P.__format__\n'
        'local (3, True)\n'
        "dict-get 'abc'\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "super_object", source, expected)


def test_super_stops_at_an_unported_builtin_base(tmp_path, monkeypatch, pcc_runtime_archive):
    # CPython answers these from BaseException's and dict's own namespaces,
    # where pcc has no native with_traceback or fromkeys yet. The walk must
    # stop at the builtin base with an explicit error rather than continue
    # to object's namespace or report a plain AttributeError.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class Traced(ValueError):
            def retrace(self):
                return super().with_traceback(None)


        class Keys(dict):
            def build(self):
                return super().fromkeys("ab")


        def show(label, thunk):
            try:
                print(label, repr(thunk()))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            show("exc", lambda: Traced("boom").retrace())
            show("dict", lambda: Keys().build())


        main()
        """
    )
    expected = (
        "exc NotImplementedError super(): attribute 'with_traceback' of builtin"
        " base 'BaseException' is not implemented natively\n"
        "dict NotImplementedError super(): attribute 'fromkeys' of builtin base"
        " 'dict' is not implemented natively\n"
    )
    _run_on_five_collectors(tmp_path, "super_builtin_gap", source, expected)


def test_object_members_survive_collection_pressure(tmp_path, monkeypatch, pcc_runtime_archive):
    # The member entries hold the receiver and arguments across allocating
    # calls; a low debt threshold runs collections (and the relocating
    # collectors' moves) between them.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    monkeypatch.setenv("PCC_GC_DEBT_THRESHOLD", "1024")

    source = textwrap.dedent(
        """
        class P:
            def __init__(self, n):
                super().__init__()
                self.n = n

            def __repr__(self):
                return "P" + str(super().__repr__().startswith("<"))

            def __eq__(self, other):
                return super().__eq__(other)

            def __hash__(self):
                return super().__hash__() * 0 + self.n

            def __setattr__(self, name, value):
                super().__setattr__(name, value)

            def fmt(self, spec):
                return super().__format__(spec)


        class Q:
            pass


        def churn(k):
            junk = []
            for i in range(k):
                junk.append([str(i), (i, i + 1), {"k": i}])
            return len(junk)


        def main():
            total = 0
            ok = 0
            for i in range(1000):
                p = P(i)
                q = Q()
                total += churn(5)
                if repr(p) == "PTrue" and p == p and p.__eq__(q) is NotImplemented and hash(p) == i:
                    ok += 1
                if q.__ne__(q) is False and q.__ne__(p) is NotImplemented and q.__format__("") == str(q):
                    ok += 1
                if p.fmt("") == "PTrue" and q.__getstate__() is None:
                    ok += 1
                q.v = i
                if q.__getstate__() == {"v": i} and q.__getattribute__("v") == i:
                    ok += 1
                q.__setattr__("w", str(i))
                q.__delattr__("w")
                if not hasattr(q, "w"):
                    ok += 1
            print(ok, total)


        main()
        """
    )
    expected = "5000 5000\n"
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "object_members_pressure", source, expected)
