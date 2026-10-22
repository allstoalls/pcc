"""Methods of function-local classes are closures over the definition's cells."""

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


def test_function_local_class_methods_close_over_the_definition_cells(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # Methods of a function-local class are closures: each execution of the
    # class statement gives its function objects the enclosing cells, so
    # instances made anywhere, subclasses, class/static methods, properties
    # and dunders all read and write the same late-bound variables, and the
    # instances carry no hidden capture attributes.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        import gc


        def make(start):
            counter = start

            class Local:
                def __init__(self, extra):
                    self.total = counter + extra

                def bump(self):
                    nonlocal counter
                    counter += 1
                    return counter

                def read(self):
                    return counter

                @classmethod
                def kind(cls):
                    return cls.__name__ + ":" + str(counter)

                @staticmethod
                def twice():
                    return counter * 2

                @property
                def doubled(self):
                    return counter * 2

                def __len__(self):
                    return counter

                def same(self, other):
                    return isinstance(other, Local)

                def adder(self, step):
                    def add(value):
                        return value + counter + step
                    return add

            def reset(value):
                nonlocal counter
                counter = value

            return Local, reset


        def main():
            Local, reset = make(10)
            a = Local(1)
            print(a.total, a.read(), len(a))
            print(a.bump(), a.read(), Local.kind(), Local.twice(), a.doubled)
            reset(100)
            print(a.read(), len(a), Local.kind(), a.adder(5)(1))

            class Sub(Local):
                pass

            s = Sub(5)
            print(s.total, s.read(), Sub.kind(), s.same(a))
            Other, _ = make(1)
            o = Other(0)
            print(o.read(), a.read(), o.same(a), a.same(s))
            print(sorted(vars(a)))
            gc.collect()
            print(a.read(), s.read(), o.read())


        main()
        """
    ).lstrip()
    expected = _cpython_output(source)
    assert expected == (
        "11 10 10\n11 11 Local:11 22 22\n100 100 Local:100 106\n"
        "105 100 Sub:100 True\n1 100 False True\n['total']\n100 100 1\n"
    )
    _run_on_five_collectors(tmp_path, "local_class_cells", source, expected)


def test_function_local_class_with_runtime_bases(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # A class statement's bases are evaluated when it runs: a parameter, a
    # local and two executions of one class statement are real bases with
    # their methods, fields and super() chain, as in CPython.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    source = textwrap.dedent(
        """
        class Base:
            def __init__(self, value):
                self.value = value

            def total(self):
                return self.value


        def make(step):
            class Local(Base):
                def total(self):
                    return super().total() + step

            return Local


        def subclass(parent):
            class Child(parent):
                def __init__(self, value, extra):
                    super().__init__(value)
                    self.extra = extra

                def total(self):
                    return super().total() * 100 + self.extra

            return Child


        def main():
            first = make(1)
            second = make(2)
            child = subclass(first)(10, 7)
            print(child.total(), child.value, child.extra, isinstance(child, first))

            class Both(first, second):
                pass

            both = Both(10)
            print(both.total(), [cls.__name__ for cls in Both.__mro__])


        main()
        """
    ).lstrip()
    expected = _cpython_output(source)
    assert expected == "1107 10 7 True\n13 ['Both', 'Local', 'Local', 'Base', 'object']\n"
    _run_on_five_collectors(tmp_path, "local_class_bases", source, expected)


def test_class_body_binding_does_not_shadow_a_method_capture(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # A class-body binding is a class attribute: methods, properties, class
    # and static methods still close over the enclosing function's binding
    # of that name (CPython's LOAD_CLOSURE), whether a value or a def.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        def values():
            f = "enclosing"

            class C:
                f = "class attribute"

                def method(self):
                    return f, self.f

                @property
                def prop(self):
                    return f

                @classmethod
                def cm(cls):
                    return f, cls.f

                @staticmethod
                def sm():
                    return f

            c = C()
            return c.method(), c.prop, C.cm(), C.sm()


        def functions():
            def f(value):
                return value

            class C:
                f = 42

                def method(self):
                    alias = f
                    return alias

            return C().method()(5), C.f


        print(values())
        print(functions())
        """
    ).lstrip()
    expected = _cpython_output(source)
    assert expected == (
        "(('enclosing', 'class attribute'), 'enclosing', "
        "('enclosing', 'class attribute'), 'enclosing')\n"
        "(5, 42)\n"
    )
    _run_on_five_collectors(tmp_path, "local_class_shadowed_capture", source, expected)
