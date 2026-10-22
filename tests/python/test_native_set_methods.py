"""set methods union/intersection/difference/symmetric_difference/issubset/
issuperset/isdisjoint/copy under strict no-libpython.

Only add/remove/discard/update were in the set-method dispatch gate
(_DYN_SET_METHOD_NATIVE), so `a.union(b)` etc. raised AttributeError (issubset/
issuperset were even implemented but unreachable through the gate). Fix
(frontend): widen the gate + handle the methods — intersection/difference/
symmetric_difference via the existing runtime helpers; union/copy via
py_set_new + py_set_update; isdisjoint via intersection length; issubset/
issuperset via py_set_issubset/issuperset. No runtime change.

Runs under ``--backend self --python-libpython=off`` in DEFAULT runtime mode.
"""
from __future__ import annotations
import os, subprocess
from pathlib import Path


def _run(tmp_path, source):
    src = tmp_path / "p.py"; src.write_text(source, encoding="utf-8")
    exe = tmp_path / "p_bin"; env = os.environ.copy(); env.pop("LC_ALL", None)
    b = subprocess.run(["uv","run","pcc","--backend","self","--python-libpython=off","--ir-scaffold=on",str(src),"-o",str(exe)], text=True, capture_output=True, timeout=420, env=env)
    assert b.returncode == 0, b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_set_methods(tmp_path):
    out = _run(tmp_path,
        "def main():\n"
        "    a = {1, 2, 3}\n"
        "    b = {2, 3, 4}\n"
        "    print(sorted(a.union(b)))\n"                  # [1, 2, 3, 4]
        "    print(sorted(a.intersection(b)))\n"           # [2, 3]
        "    print(sorted(a.difference(b)))\n"             # [1]
        "    print(sorted(a.symmetric_difference(b)))\n"   # [1, 4]
        "    print(a.issubset({1, 2, 3, 4}))\n"            # True
        "    print(a.issubset(b))\n"                       # False
        "    print({1, 2}.issuperset({1}))\n"             # True
        "    print(a.isdisjoint({5, 6}))\n"               # True
        "    print(a.isdisjoint(b))\n"                     # False
        "    print(sorted(a.copy()))\n"                    # [1, 2, 3]
        "    print(sorted(a))\n"                           # [1, 2, 3] (union didn't mutate a)
        "main()\n")
    assert out.split("\n")[:11] == [
        "[1, 2, 3, 4]", "[2, 3]", "[1]", "[1, 4]",
        "True", "False", "True", "True", "False",
        "[1, 2, 3]", "[1, 2, 3]",
    ], out


def test_set_pop_static_dynamic_and_empty(tmp_path):
    out = _run(tmp_path,
        "def main():\n"
        "    s = {42}\n"
        "    print(s.pop())\n"
        "    print(len(s))\n"
        "    t = {7}\n"
        "    fn = getattr(t, 'pop')\n"
        "    print(fn())\n"
        "    print(len(t))\n"
        "    try:\n"
        "        t.pop()\n"
        "    except KeyError:\n"
        "        print('empty')\n"
        "main()\n")
    assert out.split("\n")[:5] == ["42", "0", "7", "0", "empty"], out


def test_set_comparison_operators(tmp_path):
    """``<=``/``>=`` (subset/superset) and ``<``/``>`` (proper) operators.
    Set ordering is a PARTIAL order, so py_obj_le/lt/gt/ge special-case
    SET&&SET to py_set_issubset/issuperset instead of the total 3-way compare
    (which made ``<=`` always True and ``<`` always False)."""
    out = _run(tmp_path,
        "def main():\n"
        "    print({1, 2} <= {1, 2, 3})\n"      # True
        "    print({1, 2, 3} <= {1, 2})\n"      # False
        "    print({1, 2} <= {1, 2})\n"         # True
        "    print({1, 2} < {1, 2, 3})\n"       # True
        "    print({1, 2} < {1, 2})\n"          # False
        "    print({1, 2, 3} >= {1, 2})\n"      # True
        "    print({1, 2} >= {1, 2, 3})\n"      # False
        "    print({1, 2, 3} > {1, 2})\n"       # True
        "    print({1, 2} > {1, 2})\n"          # False
        "    print({1, 2} <= {2, 3})\n"         # False (incomparable)
        "    print({1, 2} >= {2, 3})\n"         # False (incomparable)
        "main()\n")
    assert out.split("\n")[:11] == [
        "True", "False", "True", "True", "False",
        "True", "False", "True", "False", "False", "False",
    ], out


def test_set_copy_is_a_new_set_for_typed_and_dynamic_receivers(tmp_path):
    # A set-typed copy() is union() with no arguments, published into its
    # consumer's slot (sorted(s.copy())). A dynamic receiver takes the dict
    # route's copy, which still calls a non-container's own copy().
    out = _run(tmp_path,
        "class Box:\n"
        "    def copy(self):\n"
        "        return 'box copy'\n"
        "def dup(value):\n"
        "    return value.copy()\n"
        "def main():\n"
        "    a = {1, 2, 3}\n"
        "    b = a.copy()\n"
        "    b.add(9)\n"
        "    print(sorted(a), sorted(b), b is a)\n"
        "    c = dup(a)\n"
        "    c.discard(1)\n"
        "    print(sorted(a), sorted(c), sorted(dup(set())))\n"
        "    print(dup(Box()))\n"
        "main()\n")
    assert out.split("\n")[:3] == [
        "[1, 2, 3] [1, 2, 3, 9] False",
        "[1, 2, 3] [2, 3] []",
        "box copy",
    ], out
