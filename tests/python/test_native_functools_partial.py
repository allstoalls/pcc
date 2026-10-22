"""Native ``functools.partial`` under strict no-libpython (run-based).

``functools.partial`` is the single largest ``--python-libpython=auto`` fallback
item in the numpy import diagnostic and a common stdlib feature. It lowers to the
runtime ``py_functools_partial`` (a PyFuncObject whose entry concatenates the
captured args with the call args); the function argument is emitted with
``_prefer_native_callable_values`` so a top-level Dyn-typed ``def`` boxes into a
native PyFuncObject (the path closures/lambdas use), not a libpython callable.

These tests COMPILE + RUN under ``--backend self --python-libpython=off`` (which
hard-errors on any residual ``py_cpy_*`` fallback), so a green run proves the
whole path is native end-to-end and produces the right value.

Both the ``functools.partial(...)`` attribute form and the ``from functools
import partial`` form are native at module level and inside function bodies,
for both Dyn-typed and fully type-annotated ``fn``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _run_pcc_program(tmp_path: Path, source: str) -> str:
    src = tmp_path / "prog.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=300, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_functools_partial_attr_form_native_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "import functools\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "print(functools.partial(add, 10)(5))\n"
        "print(functools.partial(add, 100)(1))\n",
    )
    assert out.split() == ["15", "101"], out


def test_functools_partial_from_import_native_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "from functools import partial\n"
        "def mul(a, b):\n"
        "    return a * b\n"
        "print(partial(mul, 6)(7))\n",
    )
    assert out.strip() == "42", out


def test_functools_partial_typed_fn_native_no_libpython(tmp_path):
    # A fully type-annotated top-level fn also boxes natively at module level.
    out = _run_pcc_program(
        tmp_path,
        "import functools\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "print(functools.partial(add, 10)(5))\n",
    )
    assert out.strip() == "15", out


def test_functools_partial_inside_function_native_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "import functools\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "def use():\n"
        "    return functools.partial(add, 10)(5)\n"
        "print(use())\n",
    )
    assert out.strip() == "15", out


def test_functools_partial_from_import_inside_function_native_no_libpython(tmp_path):
    # The ``from functools import partial`` form INSIDE a function body.
    out = _run_pcc_program(
        tmp_path,
        "from functools import partial\n"
        "def mul(a, b):\n"
        "    return a * b\n"
        "def use():\n"
        "    return partial(mul, 6)(7)\n"
        "print(use())\n",
    )
    assert out.strip() == "42", out


def test_functools_partial_kwargs_calls_native_function_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "import functools\n"
        "def f(a, b, c=3, **kw):\n"
        "    print(a, b, c, kw.get('x'))\n"
        "p = functools.partial(f, b=2, x=9)\n"
        "p(1)\n",
    )
    assert out.strip() == "1 2 3 9", out


def test_functools_partial_result_stays_dynamic_with_provider_class_export():
    """The native partial projection is PyFunc, never the scaffold class."""
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import Assign, DynType, Name
    from pcc.frontends.python.type_infer import infer_module

    module = parse_and_lift(
        "import functools\n"
        "def target(value):\n"
        "    return value\n"
        "bound = functools.partial(target, marker=True)\n"
        "result = bound(7)\n",
        "partial_projection.py",
        "partial_projection",
    )
    typed = infer_module(
        module,
        external_exports={
            "functools": {
                "partial": {
                    "kind": "class",
                    "class_name": "partial",
                    "base_names": (),
                    "field_names": ("_fn", "_args", "_kwargs"),
                    "field_types": (),
                }
            }
        },
    )

    bound = next(
        stmt
        for stmt in typed.body
        if isinstance(stmt, Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], Name)
        and stmt.targets[0].ident == "bound"
    )
    assert isinstance(bound.value.ty, DynType)


def test_functools_partial_compiled_sibling_function_no_libpython(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "provider.py").write_text(
        "import functools\n"
        "def array_function_dispatch(dispatcher=None, module=None):\n"
        "    def decorator(implementation):\n"
        "        return implementation\n"
        "    return functools.partial(decorator)\n",
        encoding="utf-8",
    )
    (pkg / "consumer.py").write_text(
        "import functools\n"
        "from . import provider\n"
        "array_function_dispatch = functools.partial(\n"
        "    provider.array_function_dispatch, module='pkg')\n"
        "def implementation():\n"
        "    return 7\n"
        "wrapped = array_function_dispatch()(implementation)\n"
        "print(wrapped())\n",
        encoding="utf-8",
    )
    (pkg / "__main__.py").write_text(
        "from . import consumer\n",
        encoding="utf-8",
    )
    exe = tmp_path / "pkg_main"
    repo_root = Path.cwd()
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(repo_root / "scripts" / "pcc_multi.py"),
            "--entry",
            "pkg.__main__",
            "--out",
            str(exe),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(pkg / "__main__.py") + "=pkg.__main__",
            str(pkg / "consumer.py") + "=pkg.consumer",
            str(pkg / "provider.py") + "=pkg.provider",
            str(repo_root / "pcc" / "stdlib" / "functools.py")
            + "=functools",
        ],
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "7", run.stdout


def test_functools_partial_async_function_returns_coroutine_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "import asyncio\n"
        "import functools\n"
        "async def f(a, b=3, **kw):\n"
        "    print(a, b, kw.get('x'))\n"
        "p = functools.partial(f, b=2, x=9)\n"
        "asyncio.run(p(1))\n",
    )
    assert out.strip() == "1 2 9", out


def test_package_method_default_async_function_partial_no_libpython(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "server.py").write_text(
        "import asyncio\n"
        "import functools\n"
        "\n"
        "async def handler(a, b=3, **kw):\n"
        "    print(a, b, kw.get('x'))\n"
        "\n"
        "class Server:\n"
        "    def start(self, cb=handler):\n"
        "        task = functools.partial(cb, b=2, x=9)\n"
        "        asyncio.run(task(1))\n",
        encoding="utf-8",
    )
    (pkg / "__main__.py").write_text(
        "from .server import Server\n"
        "Server().start()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "pkg_main"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(pkg / "__main__.py"), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=300, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "1 2 9", run.stdout


def test_package_main_method_default_async_function_partial_no_libpython(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "server.py").write_text(
        "import asyncio\n"
        "import functools\n"
        "\n"
        "async def handler(a, b=3, **kw):\n"
        "    print(a, b, kw.get('x'))\n"
        "\n"
        "class Server:\n"
        "    def start(self, cb=handler):\n"
        "        task = functools.partial(cb, b=2, x=9)\n"
        "        asyncio.run(task(1))\n"
        "\n"
        "def main():\n"
        "    Server().start()\n",
        encoding="utf-8",
    )
    (pkg / "__main__.py").write_text(
        "from .server import main\n"
        "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "pkg_main"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(pkg / "__main__.py"), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=300, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "1 2 9", run.stdout


def test_dynamic_list_method_default_async_function_partial_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "import asyncio\n"
        "import functools\n"
        "\n"
        "async def handler(a, b=3, **kw):\n"
        "    print(a, b, kw.get('x'))\n"
        "\n"
        "class Server:\n"
        "    def start(self, cb=handler):\n"
        "        task = functools.partial(cb, b=2, x=9)\n"
        "        asyncio.run(task(1))\n"
        "\n"
        "items = [Server()]\n"
        "for option in items:\n"
        "    option.start()\n",
    )
    assert out.strip() == "1 2 9", out


def test_super_init_with_pyfunc_method_table_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class Base:\n"
        "    def __init__(self, value=None):\n"
        "        self.value = value\n"
        "\n"
        "class Child(Base):\n"
        "    def __init__(self):\n"
        "        super().__init__('ok')\n"
        "\n"
        "print(Child().value)\n",
    )
    assert out.strip() == "ok", out


def test_collections_namedtuple_metadata_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "from collections import namedtuple\n"
        "\n"
        "Pair = namedtuple('Pair', 'left right')\n"
        "pair = Pair(1, right=2)\n"
        "print(Pair._fields)\n"
        "print(pair.left, pair.right)\n"
        "print(pair._asdict().get('right'))\n"
        "print(repr(pair))\n",
    )
    assert out.strip().splitlines() == [
        "('left', 'right')",
        "1 2",
        "2",
        "Pair(left=1, right=2)",
    ], out


def test_class_body_method_alias_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class Stream:\n"
        "    def encrypt(self, s):\n"
        "        return 'enc:' + s\n"
        "    decrypt = encrypt\n"
        "\n"
        "stream = Stream()\n"
        "print(stream.encrypt('x'))\n"
        "print(stream.decrypt('y'))\n",
    )
    assert out.strip().splitlines() == ["enc:x", "enc:y"], out


def test_class_body_chained_method_alias_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class Aead:\n"
        "    def process(self, s):\n"
        "        return 'proc:' + s\n"
        "    encrypt_and_digest = decrypt_and_verify = process\n"
        "\n"
        "a = Aead()\n"
        "print(a.encrypt_and_digest('x'))\n"
        "print(a.decrypt_and_verify('y'))\n",
    )
    assert out.strip().splitlines() == ["proc:x", "proc:y"], out


def test_class_body_attr_references_prior_attr_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class AES:\n"
        "    g1 = bytes([1, 2, 128])\n"
        "    g2 = [a << 1 & 0xff ^ 0x1b if a & 0x80 else a << 1 for a in g1]\n"
        "    g3 = [a ^ (a << 1 & 0xff ^ 0x1b if a & 0x80 else a << 1) for a in g1]\n"
        "\n"
        "print(AES.g2)\n"
        "print(AES.g3)\n",
    )
    assert out.strip().splitlines() == ["[2, 4, 27]", "[3, 6, 155]"], out


def test_runtime_bytes_from_base64_are_iterable_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "import base64\n"
        "\n"
        "g1 = base64.b64decode(b'AQKA')\n"
        "print([a << 1 & 0xff ^ 0x1b if a & 0x80 else a << 1 for a in g1])\n"
        "\n"
        "class AES:\n"
        "    g1 = base64.b64decode(b'AQKA')\n"
        "    g2 = [a << 1 & 0xff ^ 0x1b if a & 0x80 else a << 1 for a in g1]\n"
        "\n"
        "print(AES.g2)\n",
    )
    assert out.strip().splitlines() == ["[2, 4, 27]", "[2, 4, 27]"], out


def test_class_body_tuple_unpack_attrs_are_visible_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class Camellia:\n"
        "    S1 = bytes([1, 2, 3, 4])\n"
        "    S2, S3, S4 = bytes(i for i in S1), bytes(i + 10 for i in S1), S1[::2] + S1[1::2]\n"
        "    S = (S1, S4, S3, S2)\n"
        "\n"
        "print(Camellia.S)\n",
    )
    assert out.strip() == "(b'\\x01\\x02\\x03\\x04', b'\\x01\\x03\\x02\\x04', b'\\x0b\\x0c\\r\\x0e', b'\\x01\\x02\\x03\\x04')", out


def test_class_body_attr_reassignment_sees_previous_value_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class Camellia:\n"
        "    KS = bytes([1, 2, 3, 4])\n"
        "    KS = tuple(i + 10 for i in KS)\n"
        "\n"
        "print(Camellia.KS)\n",
    )
    assert out.strip() == "(11, 12, 13, 14)", out
