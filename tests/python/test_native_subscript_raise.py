"""dict[missing] -> KeyError and list[oob] -> IndexError under no-libpython.

The statically-typed subscript path (subscript_lowering.py DictType/ListType
branches) called py_dict_get / py_list_get, which return NULL silently on a
missing key / out-of-range index. So `d['missing']` / `a[9]` produced "<null>"
and a surrounding try/except could not catch anything (no exception was
raised).

Fix: new raising subscript variants py_dict_getitem (KeyError carrying the key,
via py_exc_new_with_value) and py_list_getitem (IndexError "list index out of
range"), mirrored in C (py_dict.c / py_list.c) and the pcc-Python ports
(py_dict.py / py_list.py). subscript_lowering routes d[k]/a[i] to them and emits
the post-call err check so try/except catches the raise. py_dict_get /
py_list_get stay non-raising for dict.get()/pop()/setdefault() and other
internal callers.

Compiles + runs under ``--backend self --python-libpython=off`` in DEFAULT
runtime mode (pcc-Python ports — the goal mode).
"""
from __future__ import annotations

import inspect
import os
import re
import subprocess
from pathlib import Path


def _compile_to_ll(tmp_path: Path, source: str, name: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    out = tmp_path / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
    )
    return out.read_text(encoding="utf-8")


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
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_dict_keyerror_list_indexerror_catch_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "def main():\n"
        "    d = {'a': 1, 'b': 2}\n"
        "    a = [10, 20, 30]\n"
        "    print(d['a'], a[1], a[-1])\n"        # present/valid/negative -> 1 20 30
        "    try:\n"
        "        print(d['z'])\n"
        "    except KeyError:\n"
        "        print('caught-KeyError')\n"
        "    try:\n"
        "        print(a[9])\n"
        "    except IndexError:\n"
        "        print('caught-IndexError')\n"
        "    # no-regression: dict.get/pop/setdefault keep using non-raising py_dict_get\n"
        "    print(d.get('z', 99), d.get('a'))\n"
        "    print(d.pop('b'), d.setdefault('c', 3))\n"
        "main()\n",
    )
    assert out.split("\n")[:5] == [
        "1 20 30",
        "caught-KeyError",
        "caught-IndexError",
        "99 1",
        "2 3",
    ], out


def test_exact_container_getitem_has_one_behavior_owner():
    from pcc.py_frontend.codegen.exact_int_lowering import ExactIntLoweringMixin
    from pcc.py_frontend.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.py_frontend.codegen.subscript_lowering import SubscriptLoweringMixin

    owner = inspect.getsource(
        SubscriptLoweringMixin._emit_exact_container_subscript_load_object
    )
    ordinary = inspect.getsource(SubscriptLoweringMixin._emit_subscript_load)
    object_boundary = inspect.getsource(ExactIntLoweringMixin._emit_subscript_load_object)

    for runtime_symbol in (
        "py_list_getitem",
        "py_tuple_getitem",
        "py_dict_getitem",
    ):
        assert runtime_symbol in owner
        assert runtime_symbol not in ordinary
        assert runtime_symbol not in object_boundary
    assert "_emit_post_call_err_check" in owner
    assert "_emit_exact_container_subscript_load_object" in ordinary
    assert "_emit_exact_container_subscript_load_object" in object_boundary
    assert "_emit_exact_container_subscript_load_object" in L1_CODEGEN_HOST_METHODS


def test_exact_container_getitem_former_entrypoints_emit_same_raising_shape(tmp_path):
    source = (
        "def list_value(xs: list[int], i: int) -> int:\n"
        "    return xs[i] + 1\n"
        "def list_object(xs: list[int], i: int):\n"
        "    return [xs[i]]\n"
        "def dict_value(values: dict[str, int], key: str) -> int:\n"
        "    return values[key] + 1\n"
        "def dict_object(values: dict[str, int], key: str):\n"
        "    return [values[key]]\n"
    )
    ir_text = _compile_to_ll(tmp_path, source, "exact_container_owner")

    def function_body(name: str) -> str:
        marker = "@user_exact_container_owner_" + name + "("
        marker_pos = ir_text.index(marker)
        start = ir_text.rfind("define ", 0, marker_pos)
        end = ir_text.index("\n}", marker_pos)
        return ir_text[start:end]

    for name in ("list_value", "list_object"):
        body = function_body(name)
        assert "@py_list_getitem" in body
        assert "@py_err_occurred" in body
        assert "subscript.list.getitem" in body
    for name in ("dict_value", "dict_object"):
        body = function_body(name)
        assert "@py_dict_getitem" in body
        assert "@py_err_occurred" in body
        assert "subscript.dict.getitem" in body


def test_dynamic_int_subscript_keeps_python_keys(tmp_path):
    source = (
        "def get_item(o, i: int):\n"
        "    return o[i]\n"
        "def set_item(o, i: int, v):\n"
        "    o[i] = v\n"
        "def main():\n"
        "    xs = [4, 5]\n"
        "    tup = (7, 8)\n"
        "    d = {1: 'one'}\n"
        "    print(get_item(xs, 1))\n"
        "    print(get_item(tup, 0))\n"
        "    print(get_item(d, 1))\n"
        "    set_item(xs, 0, 9)\n"
        "    set_item(d, 2, 'two')\n"
        "    print(xs[0], d[2])\n"
        "main()\n"
    )
    ir_text = _compile_to_ll(tmp_path, source, "dyn_int_subscript_keys")
    assert re.search(r"call[^\n]*@py_obj_subscript\(", ir_text)
    assert re.search(r"call[^\n]*@py_obj_setitem\(", ir_text)
    assert not re.search(r"call[^\n]*@py_obj_(?:subscript|setitem)_i64\(", ir_text)
    out = _run_pcc_program(tmp_path, source)
    assert out.splitlines() == ["5", "7", "one", "9 two"], out
