"""dict.fromkeys(iterable[, value]) under strict no-libpython (run-based).

dict.fromkeys (a builtin-type classmethod) routed through the libpython
fallback (_maybe_emit_builtin_type_method), so it was rejected under
--python-libpython=off. Added a native branch + runtime py_dict_fromkeys
(py_dict.c + port .py): iterate the iterable via the iterator protocol
(py_obj_iter/py_obj_next, clearing a terminal StopIteration like sorted()) and
py_dict_set each key to value (None when omitted).

Compiles + runs under ``--backend self --python-libpython=off`` in DEFAULT
runtime mode (pcc-Python ports — the goal mode) and asserts CPython-exact output.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


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


def test_dict_fromkeys_native_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class BadIter:\n"
        "    def __iter__(self):\n"
        "        raise ValueError('iter boom')\n"
        "class BadNext:\n"
        "    def __iter__(self):\n"
        "        return self\n"
        "    def __next__(self):\n"
        "        raise ValueError('next boom')\n"
        "def main():\n"
        "    print(dict.fromkeys(['a', 'b', 'c'], 0))\n"       # {'a': 0, 'b': 0, 'c': 0}
        "    print(dict.fromkeys([1, 2]))\n"                   # {1: None, 2: None} (default None)
        "    print(dict.fromkeys('ab', 1))\n"                  # {'a': 1, 'b': 1} (str iterable)
        "    print(dict.fromkeys(range(3)))\n"                 # {0: None, 1: None, 2: None} (range)
        "    print(dict.fromkeys([]))\n"                       # {}
        "    print(dict.fromkeys(['x', 'y', 'x', 'z']))\n"     # dup key + default None -> single entry
        "    print(dict.fromkeys(['x', 'y', 'x', 'z'], 7))\n"  # dup key + explicit value
        "    print(dict.fromkeys([1, 1, 1]))\n"                # all-dup -> {1: None}
        "    try:\n"
        "        dict.fromkeys(BadIter())\n"
        "        print('iter-missed')\n"
        "    except ValueError:\n"
        "        print('iter-error')\n"
        "    try:\n"
        "        dict.fromkeys(BadNext())\n"
        "        print('next-missed')\n"
        "    except ValueError:\n"
        "        print('next-error')\n"
        "main()\n",
    )
    assert out.split("\n")[:10] == [
        "{'a': 0, 'b': 0, 'c': 0}",
        "{1: None, 2: None}",
        "{'a': 1, 'b': 1}",
        "{0: None, 1: None, 2: None}",
        "{}",
        "{'x': None, 'y': None, 'z': None}",
        "{'x': 7, 'y': 7, 'z': 7}",
        "{1: None}",
        "iter-error",
        "next-error",
    ], out


def test_dict_fromkeys_mirrors_reject_iterator_errors_before_returning_dict():
    py_source = (
        REPO / "pcc" / "runtime" / "py" / "py_dict.py"
    ).read_text(encoding="utf-8")

    py_body = py_source.split("def py_dict_fromkeys", 1)[1].split(
        "def py_dict_pop", 1
    )[0]

    for body in (py_body,):
        assert "py_runtime_error_if_unset" in body
        assert "dict.fromkeys could not create an iterator" in body
        assert "dict.fromkeys iterator returned NULL without an exception" in body
        assert body.count("py_err_occurred()") >= 2
        assert body.index("py_runtime_error_if_unset") < body.index("py_decref(d)")
