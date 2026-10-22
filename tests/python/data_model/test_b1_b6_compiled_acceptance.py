from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def _compile_and_run(
    tmp_path: Path,
    source: str,
    *,
    extra_files: dict[str, str] | None = None,
    backend: str = "0",
) -> list[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    for name, content in (extra_files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = backend
    env["PCC_PYTHON_LIBPYTHON"] = "off"
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()


def test_b1_bytes_literal_native_bytes_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        r'''
        x = b"A\xffZ"
        print(len(x))
        print(x[0])
        print(x[1])
        print(x[1:] == b"\xffZ")
        ''',
    )
    assert lines == ["3", "65", "255", "True"]


def test_b2_type_builtin_and_name_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        class C:
            pass

        print(type(1).__name__)
        print(type([]).__name__)
        print(type(C()).__name__)
        ''',
    )
    assert lines == ["int", "list", "C"]


def test_b3_class_level_variable_read_write_shadow_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        class Base:
            count = 1

        class Child(Base):
            pass

        print(Base.count)
        print(Child.count)
        Child.count = 3
        print(Base.count)
        print(Child.count)
        Base.count = 4
        print(Base.count)
        print(Child.count)
        ''',
    )
    assert lines == ["1", "1", "1", "3", "4", "3"]


def test_b4_user_dunders_iter_hash_str_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        class It:
            def __init__(self):
                self.i = 0

            def __str__(self):
                return "it"

            def __hash__(self):
                return 12345

            def __iter__(self):
                return self

            def __next__(self):
                if self.i >= 3:
                    raise StopIteration
                self.i = self.i + 1
                return self.i

        print(str(It()))
        print(hash(It()))
        for x in It():
            print(x)
        ''',
    )
    assert lines == ["it", "12345", "1", "2", "3"]


def test_b5_exception_chaining_and_traceback_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        def f():
            try:
                raise ValueError("root")
            except ValueError as e:
                raise RuntimeError("outer") from e

        try:
            f()
        except RuntimeError as e:
            print(type(e).__name__)
            print(type(e.__cause__).__name__)
            print(str(e.__cause__))
        ''',
    )
    assert lines == ["RuntimeError", "ValueError", "root"]


def test_b6_call_splat_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        def f(a, b, c=0, d=0):
            print(a + b + c + d)

        args = (1, 2)
        kwargs = {"c": 3, "d": 4}
        f(*args, **kwargs)
        ''',
    )
    assert lines == ["10"]


def test_b6_module_attr_write_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        import mod_b6
        print(mod_b6.x)
        mod_b6.x = 7
        print(mod_b6.x)
        ''',
        extra_files={
            "mod_b6.py": '''
            x = 2
            ''',
        },
    )
    assert lines == ["2", "7"]
