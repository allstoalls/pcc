from __future__ import annotations

import os
import subprocess


def test_list_sort_key_detects_callback_mutation(tmp_path):
    source = tmp_path / "sort_key_mutation.py"
    source.write_text(
        "class Item:\n"
        "    def __init__(self, v):\n"
        "        self.v = v\n"
        "        self.owner = []\n"
        "def rank(item):\n"
        "    item.owner.append(Item(99))\n"
        "    return item.v\n"
        "def main():\n"
        "    items = [Item(3), Item(1), Item(2)]\n"
        "    for item in items:\n"
        "        item.owner = items\n"
        "    try:\n"
        "        items.sort(key=rank)\n"
        "    except ValueError as exc:\n"
        "        print(type(exc).__name__ + ': ' + str(exc))\n"
        "    print([item.v for item in items])\n"
        "main()\n",
        encoding="utf-8",
    )
    executable = tmp_path / "sort_key_mutation.bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self",
            "--python-libpython=off", "--ir-scaffold=on",
            str(source), "-o", str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    run_env = {**env, "PCC_GC_BACKEND": "4"}
    run = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=run_env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "ValueError: list modified during sort",
        "[1, 2, 3]",
    ]


def test_dynamic_list_sort_with_callable_key_at_module_scope(tmp_path):
    source = tmp_path / "main.py"
    source.write_text(
        "def rank(value):\n"
        "    return value\n"
        "groups = {'numbers': {3, 1, 2}}\n"
        "for group_name in groups.keys():\n"
        "    values = list(groups[group_name])\n"
        "    values.sort(key=lambda value: rank(value))\n"
        "print(values)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    compile_result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout.strip() == "[1, 2, 3]"


def test_sorted_iterable_with_named_callable_key_at_module_scope(tmp_path):
    source = tmp_path / "main.py"
    source.write_text(
        "def rank(value):\n"
        "    return value\n"
        "values = dict.fromkeys([3, 1, 2])\n"
        "ordered = [0] + sorted(values, key=rank)\n"
        "print(ordered)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    compile_result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout.strip() == "[0, 1, 2, 3]"


def test_list_sort_with_key_is_not_quadratic_and_is_stable(tmp_path):
    """`sort(key=...)` must lower to the merge sort, not insertion sort.

    The keyless path was moved off insertion sort long ago (see the comment in
    `py_obj_sorted`), but the keyed path was left behind, and insertion sort
    re-evaluates the key on every comparison: n**2 key calls.  Stack-map
    planning sorts ~354 roots 12186 times per oversized module, which measured
    13787 ms vs 260 ms under pcc1 for the same result.

    Guard both properties the replacement has to keep: the emitted lowering
    routes through `py_obj_sorted`, and equal keys preserve input order.
    """
    import subprocess
    import sys

    src = tmp_path / "sortkey.py"
    src.write_text(
        "class Rec:\n"
        "    def __init__(self, k: int, tag: str) -> None:\n"
        "        self.k = k\n"
        "        self.tag = tag\n"
        "\n"
        "\n"
        "def rkey(r) -> int:\n"
        "    return r.k\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        "    recs: list = []\n"
        "    i: int = 0\n"
        "    while i < 12:\n"
        "        recs.append(Rec(i % 3, 't' + str(i)))\n"
        "        i = i + 1\n"
        "    recs.sort(key=rkey)\n"
        "    out: str = ''\n"
        "    j: int = 0\n"
        "    while j < 12:\n"
        "        out = out + recs[j].tag + ','\n"
        "        j = j + 1\n"
        "    print(out)\n"
        "    return 0\n"
        "\n"
        "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "sortkey.bin"
    ll = tmp_path / "sortkey.ll"
    # Two invocations: --emit-llvm short-circuits before producing a binary.
    subprocess.run(
        [sys.executable, "-m", "pcc", str(src), f"--emit-llvm={ll}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "pcc", str(src), "-o", str(exe)],
        check=True, capture_output=True,
    )
    ir_text = ll.read_text(encoding="utf-8")
    # A callable key sorts in the runtime's rooted merge sort, which calls
    # each key once and keeps equal keys in input order.
    assert "@py_list_sort_keyed(" in ir_text, "keyed sort must use the runtime merge sort"

    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    # CPython's stable order for keys 0,1,2,0,1,2,...
    assert out.stdout.strip() == "t0,t3,t6,t9,t1,t4,t7,t10,t2,t5,t8,t11,"
