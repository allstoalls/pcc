"""Executable constraints for the AGENTS.md *Project Intent* (the north star).

This file turns the seven design obligations in ``AGENTS.md`` (and the
microkernel layering rule) into *enforceable tests* — independent of whether each
capability is fully implemented. Where the intent is mechanically checkable now
(a CLI flag, a baseline-JSON field, a source anti-pattern, an enum, a header
symbol) the test is a fast PASS that locks the contract. Where it needs a full
compile / differential / multi-backend run, the test is ``integration``
(excluded from the default suite); and where the intent is *not yet met*, the
constraint is ``xfail`` so it documents the gap in red and auto-flips to XPASS
when the gap closes.

Industrial methodologies this mirrors (the research surfaced these):
  * Self-host fixed point / reproducible build — GCC ``make compare``, Go
    ``toolchain2==toolchain3``, Rust stage0→1→2 (Obl. 1/4/5).
  * Differential / oracle testing — CPython as semantics oracle; csmith/EMI for
    C compilers; "compile twice and diff" (Obl. 2/7 — the big parametrized
    ``TestPythonSemanticsDifferential`` corpus below).
  * Golden / snapshot IR — LLVM ``lit``+``FileCheck``; Rust ``compiletest`` UI
    snapshots; pcc asserts IR *shape* (no boxing on hot paths) (Obl. 2).
  * Conformance suites — Java JCK/TCK+jtreg; CPython ``Lib/test``; Go spec tests
    (Obl. 3/7).
  * Multi-collector GC torture — Go ``GODEBUG=gccheckmark``; HotSpot GC modes;
    write-barrier verification; one shared object-graph contract (Obl. 6).
  * Semantics-preserving optimization — Alive2 / translation validation /
    metamorphic testing; a slow path that preserves semantics (Obl. 2/7).
  * "No silent fallback" negative tests — assert a forbidden path was not taken
    (Obl. 1/4).

Markers:
  intent       — fast static contract/lint locks (run by default).
  integration  — heavy compile/differential/multi-backend constraints.

Every behavioural case below was probed against CPython on 2026-06-18 and
classified before being added (green=MATCH; gap=xfail with the reproduced
failure). Several intents assumed unimplemented turned out to already work and
were therefore added as green constraints, NOT faked red — probe before marking,
in both directions.

Run:  uv run pytest tests/python/test_intent_constraints.py -m intent       -n0
      uv run pytest tests/python/test_intent_constraints.py -m integration  -n0
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.diagnostics.dependency_verdict import probe_executable_dependency


def _find_repo_root() -> Path:
    """Walk up to the repo root (dir holding AGENTS.md + the ``pcc`` pkg).

    Robust against pytest import-mode quirks where counting ``parents[N]``
    resolves one level too high.
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "AGENTS.md").is_file() and (parent / "pcc").is_dir():
            return parent
    raise RuntimeError(f"cannot locate pcc repo root from {here}")


REPO_ROOT = _find_repo_root()


# --------------------------------------------------------------------------- #
# file/source helpers (used by the fast `intent` locks)
# --------------------------------------------------------------------------- #
def _read(rel: str) -> str:
    p = REPO_ROOT / rel
    assert p.is_file(), f"expected repo file missing: {rel}"
    return p.read_text(encoding="utf-8", errors="replace")


def _iter_source_files(*rel_dirs: str, suffixes=(".py",)):
    for rel in rel_dirs:
        base = REPO_ROOT / rel
        if base.is_file():
            if base.suffix in suffixes:
                yield rel, base.read_text(encoding="utf-8", errors="replace")
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in suffixes or "__pycache__" in path.parts:
                continue
            yield (
                str(path.relative_to(REPO_ROOT)),
                path.read_text(encoding="utf-8", errors="replace"),
            )


# --------------------------------------------------------------------------- #
# compile/run helpers (used by the `integration` constraints)
# --------------------------------------------------------------------------- #
def _pcc_env(gc_backend=None, *, extra=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    if gc_backend is not None:
        env["PCC_GC_BACKEND"] = str(gc_backend)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _compile(
    tmp_path,
    source,
    *,
    backend="self",
    gc_backend=None,
    expect_ok=True,
    env_extra=None,
):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "prog.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    proc = subprocess.run(
        ["uv", "run", "pcc", "--backend", backend, "--python-libpython=off",
         "--ir-scaffold=on", str(src), "-o", str(exe)],
        text=True, capture_output=True, timeout=480,
        env=_pcc_env(gc_backend, extra=env_extra),
        cwd=str(REPO_ROOT),
    )
    if expect_ok:
        assert proc.returncode == 0, f"pcc compile failed:\n{proc.stderr}"
    return exe


def _compile_result(tmp_path, source, *, backend="self", env_extra=None):
    """Run the compile and return CompletedProcess (no success assert)."""
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "prog.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    return subprocess.run(
        ["uv", "run", "pcc", "--backend", backend, "--python-libpython=off",
         "--ir-scaffold=on", str(src), "-o", str(exe)],
        text=True, capture_output=True, timeout=480,
        env=_pcc_env(extra=env_extra),
        cwd=str(REPO_ROOT),
    )


def _compile_and_run(
    tmp_path,
    source,
    *,
    backend="self",
    gc_backend=None,
    env_extra=None,
) -> str:
    exe = _compile(
        tmp_path,
        source,
        backend=backend,
        gc_backend=gc_backend,
        env_extra=env_extra,
    )
    run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60,
                         env=_pcc_env(gc_backend, extra=env_extra))
    assert run.returncode == 0, f"compiled binary failed:\n{run.stderr}"
    return run.stdout


def _run_cpython(tmp_path, source) -> str:
    src = Path(tmp_path) / "oracle.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(source, encoding="utf-8")
    run = subprocess.run([sys.executable, str(src)], text=True, capture_output=True,
                         timeout=30, env=_pcc_env())
    assert run.returncode == 0, f"cpython oracle failed:\n{run.stderr}"
    return run.stdout


# Structured binary-inspection verdict: the platform selects the inspector
# (otool on darwin, ldd elsewhere); an unavailable inspector is an explicit
# UNAVAILABLE prerequisite — never a silent "does not link libpython" claim.
# Linkage assertions stay hard once the inspector executes
# (AUD-P2-PLATFORM-BINARY-INSPECTION-VERDICT).
_INSPECTOR_NAME = "otool" if sys.platform == "darwin" else "ldd"
INSPECTOR_VERDICT = probe_executable_dependency(_INSPECTOR_NAME)


def _links_libpython(exe) -> bool:
    exe = Path(exe)
    if not exe.exists():
        return False
    if not INSPECTOR_VERDICT.available:
        pytest.fail(INSPECTOR_VERDICT.skip_reason())
    if _INSPECTOR_NAME == "otool":
        tool = [INSPECTOR_VERDICT.resolved_path, "-L", str(exe)]
    else:
        tool = [INSPECTOR_VERDICT.resolved_path, str(exe)]
    out = subprocess.run(tool, text=True, capture_output=True, timeout=30).stdout
    return bool(re.search(r"libpython|Python\.framework", out))


def _m(body: str) -> str:
    """Wrap a statement-only body in a main() so it is a complete program."""
    return "def main():\n" + body + "\nmain()\n"


# --------------------------------------------------------------------------- #
# verified case corpora (probed vs CPython on 2026-06-18)
# --------------------------------------------------------------------------- #
# Each entry: (id, source). The differential test runs pcc (--backend self
# --python-libpython=off) and CPython and asserts identical stdout.
SEMANTICS_CASES = [
    ("arith_int", _m("    print(2*3 + 7 - 4)")),
    ("arith_float", _m("    print(3.5 * 2 - 1.25)")),
    ("floordiv_mod", _m("    print(17 // 5, 17 % 5)")),
    ("power", _m("    print(2 ** 10)")),
    ("bitops", _m("    print(6 & 3, 6 | 1, 6 ^ 2, 1 << 4, 255 >> 2)")),
    ("chained_cmp", _m("    x = 5\n    print(1 < x < 10)")),
    ("ternary", _m("    x = 7\n    print('hi' if x > 3 else 'lo')")),
    ("bool_logic", _m("    print(True and False, True or False, not True)")),
    ("divmod", _m("    print(divmod(17, 5))")),
    ("abs_round", _m("    print(abs(-5), round(3.14159, 2))")),
    ("round_binary_float_ndigits", _m("    print(round(2.675, 2))")),
    ("complex_numbers", _m("    c=complex(1,2)\n    print(c.real, c.imag)")),
    ("str_concat_repeat", _m("    print('ab' + 'cd', 'x' * 3)")),
    ("str_methods", _m("    s='Hello World'\n    print(s.upper(), s.lower(), s.split())")),
    ("str_fstring", _m("    n=42\n    print(f'val={n} hex={n:x}')")),
    ("str_slice", _m("    s='abcdef'\n    print(s[1:4], s[::-1], s[-2:])")),
    ("str_join_strip", _m("    print('-'.join(['a','b','c']), '  hi  '.strip())")),
    ("str_find_replace", _m("    print('banana'.find('na'), 'banana'.replace('a','o'))")),
    ("str_format", _m("    print('{} and {}'.format(1, 2))")),
    ("list_ops", _m("    a=[1,2,3]\n    a.append(4)\n    print(a, len(a), sum(a))")),
    ("list_comp", _m("    print([x*x for x in range(5)])")),
    ("list_comp_if", _m("    print([x for x in range(10) if x%2==0])")),
    (
        "threading_thread_start",
        "import threading\nresults=[]\ndef work(n):\n    results.append(n*n)\ndef main():\n    ts=[threading.Thread(target=work,args=(i,)) for i in range(3)]\n    for t in ts: t.start()\n    for t in ts: t.join()\n    print(sorted(results))\nmain()\n",
    ),
    ("list_sort", _m("    a=[3,1,2]\n    a.sort()\n    print(a)")),
    ("list_sort_with_key", _m("    a=[(1,'b'),(2,'a')]\n    a.sort(key=lambda t:t[1])\n    print(a)")),
    ("list_negidx", _m("    a=[1,2,3]\n    print(a[-1], a[-2])")),
    ("list_nested", _m("    print([[1,2],[3,4]][1][0])")),
    ("dict_ops", _m("    d={'a':1}\n    d['b']=2\n    print(d['a'], len(d), sorted(d.keys()))")),
    ("dict_comp", _m("    print({k: k*k for k in range(4)})")),
    ("dict_get_items", _m("    d={'x':1,'y':2}\n    print(d.get('z',0), sorted(d.items()))")),
    ("set_ops", _m("    s={1,2,3}\n    s.add(4)\n    print(sorted(s), 2 in s)")),
    ("set_comp", _m("    print(sorted({x%3 for x in range(10)}))")),
    ("tuple_unpack", _m("    a,b,c = (1,2,3)\n    print(a,b,c)")),
    ("tuple_swap", _m("    a,b=1,2\n    a,b=b,a\n    print(a,b)")),
    ("for_range", _m("    t=0\n    for i in range(5): t+=i\n    print(t)")),
    ("while_loop", _m("    i=0\n    while i<5: i+=1\n    print(i)")),
    ("break_continue", _m("    r=[]\n    for i in range(10):\n        if i==3: continue\n        if i==6: break\n        r.append(i)\n    print(r)")),
    ("nested_loop", _m("    print([(i,j) for i in range(2) for j in range(2)])")),
    ("lambda_call", _m("    g=lambda x:x*2\n    print(g(21))")),
    ("enumerate", _m("    print(list(enumerate(['a','b'])))")),
    ("zip", _m("    print(list(zip([1,2],[3,4])))")),
    ("any_all", _m("    print(any([0,1]), all([1,1]), all([1,0]))")),
    ("minmax_sum", _m("    print(min([3,1,2]), max([3,1,2]), sum([1,2,3]))")),
    ("sorted_nokey", _m("    print(sorted([3,1,2]))")),
    ("sorted_with_key", _m("    print(sorted([3,1,2], key=lambda v:-v))")),
    ("reversed", _m("    print(list(reversed([1,2,3])))")),
    ("gen_expr", _m("    print(sum(x for x in range(5)))")),
    ("walrus", _m("    print([y for x in range(5) if (y:=x*2)>4])")),
    ("exc_basic", _m("    try:\n        raise ValueError('x')\n    except ValueError as e:\n        print('caught', e)")),
    ("exc_finally", _m("    try:\n        print('t')\n    finally:\n        print('f')")),
    ("exc_index", _m("    try:\n        [][0]\n    except IndexError:\n        print('idx')")),
    ("exc_key", _m("    try:\n        {}['k']\n    except KeyError:\n        print('key')")),
    ("exc_zerodiv", _m("    try:\n        1//0\n    except ZeroDivisionError:\n        print('zdiv')")),
    ("fn_recursion", "def fib(n):\n    return n if n<2 else fib(n-1)+fib(n-2)\ndef main():\n    print(fib(10))\nmain()\n"),
    ("fn_default_args", "def f(a, b=10):\n    return a+b\ndef main():\n    print(f(1), f(1,2))\nmain()\n"),
    ("fn_args_kwargs", "def f(*a, **k):\n    return sum(a)+len(k)\ndef main():\n    print(f(1,2,3,x=1))\nmain()\n"),
    ("fn_closure", "def mk(n):\n    def inner(x):\n        return x+n\n    return inner\ndef main():\n    g=mk(10)\n    print(g(5))\nmain()\n"),
    ("fn_map_user", "def sq(x):\n    return x*x\ndef main():\n    print(list(map(sq,[1,2,3])))\nmain()\n"),
    ("fn_map_builtin_str", _m("    print(list(map(str,[1,2,3])))")),
    ("fn_filter_user", "def ev(x):\n    return x%2==0\ndef main():\n    print(list(filter(ev,[1,2,3,4])))\nmain()\n"),
    ("fn_star_call", "def f(a,b,c):\n    return a+b+c\ndef main():\n    args=[1,2,3]\n    print(f(*args))\nmain()\n"),
    ("fn_returned_from_fn", "def add(a,b):\n    return a+b\ndef pick():\n    return add\ndef main():\n    g=pick()\n    print(g(3,4))\nmain()\n"),
    ("fn_passed_as_arg", "def add(a,b):\n    return a+b\ndef apply(fn,a,b):\n    return fn(a,b)\ndef main():\n    print(apply(add,3,4))\nmain()\n"),
    ("decorator_with_varargs", "def deco(f):\n    def w(*a):\n        return f(*a)+1\n    return w\n@deco\ndef add(a,b):\n    return a+b\ndef main():\n    print(add(2,3))\nmain()\n"),
    ("fn_in_list_call", "def a():\n    return 7\ndef main():\n    fns=[a]\n    print(fns[0]())\nmain()\n"),
    ("fn_in_dict_call", "def a():\n    return 1\ndef b():\n    return 2\ndef main():\n    t={'a':a,'b':b}\n    print(t['a']())\nmain()\n"),
    ("cls_basic", "class C:\n    def __init__(self,x): self.x=x\n    def get(self): return self.x\ndef main():\n    print(C(5).get())\nmain()\n"),
    ("cls_inherit_super", "class A:\n    def f(self): return 1\nclass B(A):\n    def f(self): return super().f()+1\ndef main():\n    print(B().f())\nmain()\n"),
    ("cls_dunder_repr", "class C:\n    def __init__(self,x): self.x=x\n    def __repr__(self): return 'C(%d)'%self.x\ndef main():\n    print(repr(C(7)))\nmain()\n"),
    ("cls_dunder_add", "class V:\n    def __init__(self,x): self.x=x\n    def __add__(self,o): return V(self.x+o.x)\n    def __repr__(self): return 'V%d'%self.x\ndef main():\n    print(V(1)+V(2))\nmain()\n"),
    ("gen_function", "def g():\n    yield 1\n    yield 2\ndef main():\n    print(list(g()))\nmain()\n"),
    ("gen_send", "def g():\n    x=yield 1\n    yield x\ndef main():\n    it=g()\n    print(next(it))\n    print(it.send(99))\nmain()\n"),
    ("with_context", "class CM:\n    def __enter__(self):\n        print('e'); return self\n    def __exit__(self,*a):\n        print('x')\ndef main():\n    with CM():\n        print('body')\nmain()\n"),
    # --- breadth batch (probed 2026-06-18) ---
    ("range_step", _m("    print(list(range(0,10,3)))")),
    ("range_neg", _m("    print(list(range(5,0,-1)))")),
    ("sorted_reverse", _m("    print(sorted([1,3,2], reverse=True))")),
    ("hex_oct_bin", _m("    print(hex(255), oct(8), bin(5))")),
    ("chr_ord", _m("    print(chr(65), ord('Z'))")),
    ("int_from_str", _m("    print(int('42'), int('ff',16))")),
    ("float_from_str", _m("    print(float('3.14'))")),
    ("str_of_num", _m("    print(str(42), str(3.5))")),
    ("bool_of", _m("    print(bool(0), bool(''), bool([]), bool([1]))")),
    ("pow_builtin", _m("    print(pow(2,10), pow(2,10,1000))")),
    ("min_max_args", _m("    print(max(1,2,3), min([4,5,6]))")),
    ("max_min_with_key", _m("    words=['bb','a','ccc']\n    print(max(words,key=len), min(words,key=len))")),
    ("sum_start", _m("    print(sum([1,2,3], 10))")),
    ("len_various", _m("    print(len('abc'), len([1,2]), len({'a':1}), len((1,)))")),
    ("type_name", _m("    print(type(1).__name__, type('x').__name__)")),
    ("isinstance_builtin", _m("    print(isinstance(1,int), isinstance('a',str))")),
    ("str_startswith", _m("    print('hello'.startswith('he'), 'hello'.endswith('lo'))")),
    ("str_count_index", _m("    print('banana'.count('a'), 'banana'.index('n'))")),
    ("str_pad", _m("    print('5'.zfill(3), 'x'.rjust(4,'-'), 'y'.ljust(4,'.'))")),
    ("str_case", _m("    print('Hi There'.title(), 'Hi'.swapcase(), 'aB'.casefold())")),
    ("str_splitlines", _m("    print('a\\nb\\nc'.splitlines())")),
    ("str_percent", _m("    print('%d-%s-%.2f' % (1,'x',3.14159))")),
    ("str_fstr_align", _m("    n=7\n    print(f'{n:>5}|{n:<5}|{n:^5}')")),
    ("str_in", _m("    print('ll' in 'hello', 'z' in 'hello')")),
    ("str_iter", _m("    print([c for c in 'abc'])")),
    ("list_insert_pop", _m("    a=[1,2,3]\n    a.insert(1,9)\n    print(a, a.pop())")),
    ("list_extend_idx", _m("    a=[1,2]\n    a.extend([3,4])\n    print(a, a.index(3))")),
    ("list_reverse_count", _m("    a=[1,2,2,3]\n    a.reverse()\n    print(a, a.count(2))")),
    ("list_slice_step", _m("    a=list(range(10))\n    print(a[::2], a[1::3], a[::-2])")),
    ("list_mult", _m("    print([0]*3, [1,2]*2)")),
    ("seq_concat", _m("    print([1,2]+[3,4], (1,2)+(3,4))")),
    ("nested_unpack", _m("    (a,(b,c))=(1,(2,3))\n    print(a,b,c)")),
    ("star_target_unpack", _m("    a,*b,c=[1,2,3,4,5]\n    print(a,b,c)")),
    ("dict_merge", _m("    a={'x':1}\n    b={'y':2}\n    print({**a,**b})")),
    ("dict_update_pop", _m("    d={'a':1}\n    d.update({'b':2})\n    print(sorted(d.items()), d.pop('a'))")),
    ("dict_setdefault", _m("    d={}\n    d.setdefault('k',[]).append(1)\n    print(d)")),
    ("dict_keys_values", _m("    d={'a':1,'b':2}\n    print(sorted(d.values()), 'a' in d)")),
    ("set_operators", _m("    a={1,2,3}\n    b={2,3,4}\n    print(sorted(a|b), sorted(a&b), sorted(a-b), sorted(a^b))")),
    ("frozenset", _m("    f=frozenset([1,2,2,3])\n    print(sorted(f), len(f))")),
    ("for_else", _m("    for i in range(3):\n        pass\n    else:\n        print('nobreak')")),
    ("while_else", _m("    i=0\n    while i<3:\n        i+=1\n    else:\n        print('done',i)")),
    ("nested_func_scope", "def outer():\n    x=10\n    def inner():\n        return x*2\n    return inner()\ndef main():\n    print(outer())\nmain()\n"),
    ("global_stmt", "g=0\ndef inc():\n    global g\n    g+=1\ndef main():\n    inc(); inc()\n    print(g)\nmain()\n"),
    ("nonlocal_stmt", "def counter():\n    n=0\n    def inc():\n        nonlocal n\n        n+=1\n        return n\n    return inc\ndef main():\n    c=counter()\n    print(c(), c(), c())\nmain()\n"),
    ("multi_return_unpack", "def mm(a):\n    return min(a), max(a)\ndef main():\n    lo,hi=mm([3,1,2])\n    print(lo,hi)\nmain()\n"),
    ("cls_property", "class C:\n    def __init__(self,x): self._x=x\n    @property\n    def x(self): return self._x*2\ndef main():\n    print(C(5).x)\nmain()\n"),
    ("cls_classmethod", "class C:\n    n=0\n    @classmethod\n    def bump(cls): cls.n+=1; return cls.n\ndef main():\n    print(C.bump(), C.bump())\nmain()\n"),
    ("cls_staticmethod", "class C:\n    @staticmethod\n    def add(a,b): return a+b\ndef main():\n    print(C.add(2,3))\nmain()\n"),
    ("cls_eq_hash", "class P:\n    def __init__(self,x): self.x=x\n    def __eq__(self,o): return self.x==o.x\n    def __hash__(self): return hash(self.x)\ndef main():\n    print(P(1)==P(1), P(1)==P(2))\nmain()\n"),
    ("cls_len_getitem", "class Box:\n    def __init__(self,d): self.d=d\n    def __len__(self): return len(self.d)\n    def __getitem__(self,i): return self.d[i]\ndef main():\n    b=Box([10,20,30])\n    print(len(b), b[1])\nmain()\n"),
    ("cls_iter_protocol", "class R:\n    def __init__(self,n): self.n=n\n    def __iter__(self):\n        i=0\n        while i<self.n:\n            yield i\n            i+=1\ndef main():\n    print([x for x in R(4)])\nmain()\n"),
    ("cls_call_protocol", "class Mul:\n    def __init__(self,f): self.f=f\n    def __call__(self,x): return x*self.f\ndef main():\n    d=Mul(3)\n    print(d(5))\nmain()\n"),
    ("cls_contains", "class S:\n    def __init__(self,items): self.items=items\n    def __contains__(self,x): return x in self.items\ndef main():\n    s=S([1,2,3])\n    print(2 in s, 9 in s)\nmain()\n"),
    ("cls_lt_sort", "class N:\n    def __init__(self,v): self.v=v\n    def __lt__(self,o): return self.v<o.v\n    def __repr__(self): return str(self.v)\ndef main():\n    print(sorted([N(3),N(1),N(2)]))\nmain()\n"),
    ("cls_multi_inherit", "class A:\n    def f(self): return 'a'\nclass B:\n    def g(self): return 'b'\nclass C(A,B):\n    pass\ndef main():\n    c=C()\n    print(c.f(), c.g())\nmain()\n"),
]

# Error-model fidelity: pcc must raise/handle the same exceptions as CPython.
ERROR_MODEL_CASES = [
    ("attribute_error", "class C:\n    pass\ndef main():\n    try:\n        C().nope\n    except AttributeError:\n        print('attr')\nmain()\n"),
    ("value_error", _m("    try:\n        int('xyz')\n    except ValueError:\n        print('value')")),
    ("custom_exception", "class MyErr(Exception):\n    pass\ndef main():\n    try:\n        raise MyErr('boom')\n    except MyErr as e:\n        print('custom', e)\nmain()\n"),
    ("raise_from", _m("    try:\n        try:\n            1/0\n        except ZeroDivisionError as z:\n            raise ValueError('wrap') from z\n    except ValueError as v:\n        print('from', v)")),
    ("assert_error", _m("    try:\n        assert False, 'nope'\n    except AssertionError as a:\n        print('assert', a)")),
    ("bare_reraise", _m("    try:\n        try:\n            raise KeyError('k')\n        except KeyError:\n            raise\n    except KeyError:\n        print('reraised')")),
    ("finally_on_raise", _m("    out=[]\n    try:\n        out.append('t')\n        raise ValueError()\n    except ValueError:\n        out.append('e')\n    finally:\n        out.append('f')\n    print(out)")),
    ("stopiteration", _m("    it=iter([1])\n    print(next(it))\n    try:\n        next(it)\n    except StopIteration:\n        print('stop')")),
    ("float_div_zero", _m("    try:\n        1.0/0.0\n    except ZeroDivisionError:\n        print('fdiv')")),
    ("mixed_type_add_typeerror", _m("    try:\n        1 + 'a'\n    except TypeError:\n        print('type')")),
]

# stdlib / extended-builtin breadth (probed 2026-06-18, MATCH vs CPython).
STDLIB_CASES = [
    ("math_pi_pow", "import math\ndef main():\n    print(round(math.pi,5), math.pow(2,10))\nmain()\n"),
    ("math_sqrt_floor_ceil", "import math\ndef main():\n    print(math.sqrt(16.0), math.floor(3.7), math.ceil(3.2))\nmain()\n"),
    ("math_trunc_gcd", "import math\ndef main():\n    print(math.trunc(3.9), math.gcd(12,18))\nmain()\n"),
    ("functools_reduce", "import functools\ndef main():\n    print(functools.reduce(lambda x,y:x+y,[1,2,3,4]))\nmain()\n"),
    ("abs_int_float", _m("    print(abs(-3.5), abs(3))")),
    ("str_partition", _m("    print('a=b=c'.partition('='))")),
    ("str_rsplit", _m("    print('a-b-c'.rsplit('-',1))")),
    ("str_translate", _m("    print('hello'.translate(str.maketrans('el','ip')))")),
    ("str_format_spec", _m("    print(f'{3.14159:.3f}', f'{255:08b}', f'{42:+d}')")),
    ("bytes_decode", _m("    print(b'abc'.decode('utf-8'))")),
    ("bytes_upper", _m("    b=b'hello'\n    print(b.upper())")),
    ("dict_comp_swap", _m("    print({v:k for k,v in {'a':1,'b':2}.items()})")),
    ("nested_dict_sum", _m("    d={'xs':[1,2,3]}\n    print(sum(d['xs']))")),
    ("all_any_genexpr", _m("    print(all(x>0 for x in [1,2,3]), any(x>5 for x in [1,2,3]))")),
    ("join_over_genexpr", _m("    print(','.join(str(x) for x in range(4)))")),
    ("ternary_nested", _m("    x=5\n    print('a' if x<0 else 'b' if x<10 else 'c')")),
    ("multiline_str_count", _m("    s='''line1\\nline2'''\n    print(s.count('line'))")),
    ("frozenset_dict_key", _m("    d={frozenset([1,2]):'x'}\n    print(d[frozenset([2,1])])")),
]

# Value-model behaviour (golden expected — CPython-via-pcc-package oracle is not
# importable without the full llvmlite stack, so the Python-semantic answer is
# hardcoded). Verified pcc output 2026-06-18.
VALUECLASS_CASES = [
    ("vc_eq_value", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef main():\n    print(P(1,2)==P(1,2), P(1,2)==P(1,3))\nmain()\n", "True False\n"),
    ("vc_field_access", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef main():\n    p=P(3,4)\n    print(p.x, p.y)\nmain()\n", "3 4\n"),
    ("vc_sum_fields", "import pcc\n@pcc.valueclass\nclass V:\n    a: int\n    b: int\ndef main():\n    v=V(10,20)\n    print(v.a+v.b)\nmain()\n", "30\n"),
    ("vc_nested", "import pcc\n@pcc.valueclass\nclass Pt:\n    x: int\n    y: int\n@pcc.valueclass\nclass Seg:\n    a: Pt\n    b: Pt\ndef main():\n    s=Seg(Pt(0,0),Pt(3,4))\n    print(s.b.x, s.b.y)\nmain()\n", "3 4\n"),
    # --- batch 5: value in containers / across call boundary (golden) ---
    ("vc_eq_in_list", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef main():\n    ps=[P(1,2),P(3,4)]\n    print(P(3,4) in ps)\nmain()\n", "True\n"),
    ("vc_as_arg", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef dist2(p): return p.x*p.x+p.y*p.y\ndef main():\n    print(dist2(P(3,4)))\nmain()\n", "25\n"),
    ("vc_as_return", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef mk(a,b): return P(a,b)\ndef main():\n    p=mk(3,4)\n    print(p.x, p.y)\nmain()\n", "3 4\n"),
    ("vc_dict_value", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef main():\n    d={'a':P(1,2)}\n    print(d['a'].x)\nmain()\n", "1\n"),
    ("vc_in_tuple", "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\ndef main():\n    t=(P(1,2),P(3,4))\n    print(t[1].y)\nmain()\n", "4\n"),
]

# Obligation 7: ordinary classes keep identity (id/is/weakref/__dict__/mutation/
# subclass/finalizer). Differential vs CPython.
IDENTITY_CASES = [
    ("is_same", _m("    a=[1]\n    b=a\n    print(a is b)")),
    ("is_not_distinct_literals", _m("    print([1] is [1])")),
    ("two_instances_distinct", "class C:\n    pass\ndef main():\n    print(C() is C())\nmain()\n"),
    ("alias_mutation_visible", _m("    a=[1,2]\n    b=a\n    b.append(3)\n    print(a)")),
    ("dynamic_attr", "class C:\n    pass\ndef main():\n    c=C()\n    c.foo=42\n    print(c.foo)\nmain()\n"),
    ("weakref_alive", "import weakref\nclass C:\n    pass\ndef main():\n    c=C()\n    r=weakref.ref(c)\n    print(r() is c)\nmain()\n"),
    ("isinstance_subclass", "class A:\n    pass\nclass B(A):\n    pass\ndef main():\n    print(isinstance(B(),A), isinstance(A(),B))\nmain()\n"),
    ("finalizer_fires", "class C:\n    def __del__(self):\n        print('del')\ndef main():\n    c=C()\n    c=None\n    print('after')\nmain()\n"),
]

# Obligation 7 / INT projection: int is arbitrary precision; value-lane overflow
# promotes to bignum, never wraps. Differential vs CPython.
INT_BIGNUM_CASES = [
    ("mul_overflow_i64", _m("    print(1099511627776*1099511627776)")),
    ("add_past_i64_max", _m("    print(9223372036854775807+5)")),
    ("pow_big", _m("    print(2**100)")),
    ("lshift_big", _m("    print(1<<100)")),
    ("factorial_loop", _m("    f=1\n    for i in range(1,25): f*=i\n    print(f)")),
    ("negate_big", _m("    print(-(2**70))")),
    ("bigdiv", _m("    print((2**80)//(2**40))")),
    ("sum_reduction_bignum", _m("    print(sum([10**18]*100))")),
]

# Obligation 6: same GC-semantics program identical across PCC_GC_BACKEND=0..4.
GC_PROGRAMS = [
    ("finalizer", "class C:\n    def __del__(self):\n        print('del')\ndef main():\n    c=C()\n    c=None\n    print('end')\nmain()\n"),
    ("weakref_clears", "import weakref,gc\nclass C:\n    pass\ndef main():\n    c=C()\n    r=weakref.ref(c)\n    c=None\n    gc.collect()\n    print(r() is None)\nmain()\n"),
    ("cycle_collect", "import gc\nclass N:\n    def __init__(self): self.p=None\ndef main():\n    a=N(); b=N()\n    a.p=b; b.p=a\n    a=None; b=None\n    print(gc.collect()>=0)\nmain()\n"),
    ("nested_cycle", "import gc\nclass N:\n    def __init__(self,t): self.t=t; self.kids=[]\ndef main():\n    r=N('r')\n    for i in range(3):\n        k=N(i); k.parent=r; r.kids.append(k)\n    r=None\n    gc.collect()\n    print('ok')\nmain()\n"),
    ("dict_holds_refs", "import gc\nclass C:\n    pass\ndef main():\n    d={}\n    for i in range(5): d[i]=C()\n    d=None\n    gc.collect()\n    print('ok')\nmain()\n"),
    ("del_in_loop", "class C:\n    def __init__(self,i): self.i=i\n    def __del__(self): print('d',self.i)\ndef main():\n    for i in range(3):\n        c=C(i)\n    print('done')\nmain()\n"),
    ("resurrection", "import gc\nrevived = []\nclass R:\n    def __init__(self, t): self.t=t\n    def __del__(self):\n        revived.append(self)\n        print('del', self.t)\ndef main():\n    r=R('x'); r=None\n    gc.collect()\n    print('revived', len(revived))\nmain()\n"),
    # --- batch 3 (probed 2026-06-18, GC_EQUAL across 0..4) ---
    ("set_holds_refs", "import gc\nclass C:\n    pass\ndef main():\n    s=set()\n    for i in range(5): s.add(C())\n    s=None\n    gc.collect()\n    print('ok')\nmain()\n"),
    ("tuple_of_objects", "import gc\nclass C:\n    def __init__(self,i): self.i=i\ndef main():\n    t=(C(1),C(2),C(3))\n    print(t[1].i)\n    t=None\n    gc.collect()\n    print('ok')\nmain()\n"),
    ("self_reference", "import gc\nclass N:\n    def __init__(self): self.me=self\ndef main():\n    n=N()\n    n=None\n    print(gc.collect()>=0)\nmain()\n"),
    ("deep_nesting", "import gc\nclass N:\n    def __init__(self): self.child=None\ndef main():\n    root=N()\n    cur=root\n    for i in range(20):\n        cur.child=N()\n        cur=cur.child\n    root=None\n    gc.collect()\n    print('ok')\nmain()\n"),
    ("exception_holds_ref", "import gc\nclass C:\n    def __del__(self): print('del')\ndef main():\n    try:\n        c=C()\n        raise ValueError('x')\n    except ValueError:\n        print('caught')\n    gc.collect()\n    print('end')\nmain()\n"),
    ("generator_frame_held_ref", "import gc\nclass C:\n    def __del__(self): print('del')\ndef main():\n    def g():\n        c=C()\n        yield 1\n        yield 2\n    it=g()\n    print(next(it))\n    it=None\n    gc.collect()\n    print('end')\nmain()\n"),
]

# GC cross-backend GAPS — observable output diverges across PCC_GC_BACKEND=0..4.
# This corpus is allowed to be empty when all known GC-equality gaps have been
# promoted into GC_PROGRAMS.
GC_GAP_PROGRAMS = []

# INTENT GAPS — verified red on 2026-06-18 (pcc differs from CPython here).
# Each is xfail and flips to XPASS automatically when the obligation is met.
GAP_CASES = [
]

# Value-model genexpr GAP (golden; CPython-via-pcc oracle unavailable).
VALUECLASS_GENEXPR_GAP_SRC = (
    "import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\n"
    "def main():\n    ps=[P(1,2),P(3,4),P(5,6)]\n    print(sum(p.x for p in ps))\nmain()\n"
)

# Value-model GAP (golden expected; differential-vs-CPython oracle unavailable).
VALUECLASS_GAP_SRC = (
    "import pcc\n@pcc.valueclass\nclass V:\n    x: int\n    y: int\n"
    "    def norm2(self): return self.x*self.x+self.y*self.y\n"
    "def main():\n    print(V(3,4).norm2())\nmain()\n"
)


# --------------------------------------------------------------------------- #
# Metamorphic generator (csmith-style): deterministic per seed, constrained to
# the proven-green operation set so the generated corpus diffs cleanly against
# CPython. Random *structure* stress-tests codegen by combinatorial depth.
# --------------------------------------------------------------------------- #
def _gen_int_expr(rng, depth):
    if depth <= 0 or rng.random() < 0.3:
        return str(rng.randint(0, 20))
    op = rng.choice(["+", "-", "*"])
    return f"({_gen_int_expr(rng, depth-1)} {op} {_gen_int_expr(rng, depth-1)})"


def _gen_bool_expr(rng, depth):
    if depth <= 0 or rng.random() < 0.4:
        a, b = _gen_int_expr(rng, 2), _gen_int_expr(rng, 2)
        return f"({a} {rng.choice(['<','<=','>','>=','==','!='])} {b})"
    op = rng.choice(["and", "or"])
    return f"({_gen_bool_expr(rng, depth-1)} {op} {_gen_bool_expr(rng, depth-1)})"


def _gen_program(seed):
    rng = random.Random(seed)
    kind = rng.choice(["int", "bool", "cond"])
    if kind == "int":
        expr = _gen_int_expr(rng, 4)
    elif kind == "bool":
        expr = _gen_bool_expr(rng, 3)
    else:
        expr = f"({_gen_int_expr(rng,3)} if {_gen_bool_expr(rng,2)} else {_gen_int_expr(rng,3)})"
    return f"def main():\n    print({expr})\nmain()\n"


METAMORPHIC_SEEDS = list(range(60))

# Second generator: string / list expressions (proven-green op set).
_WORDS = ["ab", "xy", "foo", "q", "12"]


def _gen_str_expr(rng, depth):
    if depth <= 0 or rng.random() < 0.4:
        return repr(rng.choice(_WORDS))
    c = rng.random()
    if c < 0.4:
        return f"({_gen_str_expr(rng, depth-1)} + {_gen_str_expr(rng, depth-1)})"
    if c < 0.7:
        return f"({_gen_str_expr(rng, depth-1)} * {rng.randint(0, 3)})"
    return f"{_gen_str_expr(rng, depth-1)}.{rng.choice(['upper()', 'lower()'])}"


def _gen_list_expr(rng, depth):
    if depth <= 0 or rng.random() < 0.5:
        return "[" + ",".join(str(rng.randint(0, 9)) for _ in range(rng.randint(0, 3))) + "]"
    if rng.random() < 0.5:
        return f"({_gen_list_expr(rng, depth-1)} + {_gen_list_expr(rng, depth-1)})"
    return f"[x*{rng.randint(1, 3)} for x in range({rng.randint(0, 4)})]"


def _gen_str_list_program(seed):
    rng = random.Random(seed)
    k = rng.choice(["str", "strlen", "list", "listsum"])
    if k == "str":
        return f"def main():\n    print({_gen_str_expr(rng, 3)})\nmain()\n"
    if k == "strlen":
        return f"def main():\n    print(len({_gen_str_expr(rng, 3)}))\nmain()\n"
    if k == "list":
        return f"def main():\n    print({_gen_list_expr(rng, 3)})\nmain()\n"
    return f"def main():\n    print(sum({_gen_list_expr(rng, 2)}))\nmain()\n"


STR_LIST_SEEDS = list(range(100, 140))

# Cross-backend determinism subset (diverse ids from SEMANTICS_CASES).
_CROSS_IDS = {
    "arith_int", "floordiv_mod", "power", "bitops", "chained_cmp",
    "str_concat_repeat", "str_methods", "str_fstring", "list_comp", "list_sort",
    "dict_ops", "set_ops", "tuple_unpack", "for_range", "break_continue",
    "fn_recursion", "fn_closure", "fn_args_kwargs", "cls_basic",
    "cls_inherit_super", "cls_dunder_add", "gen_function", "with_context",
}
CROSS_BACKEND_CASES = [(i, s) for i, s in SEMANTICS_CASES if i in _CROSS_IDS]


# =========================================================================== #
# Fast `intent` locks — pure file reads, run by default.
# =========================================================================== #
@pytest.mark.intent
def test_repo_root_resolves():
    assert (REPO_ROOT / "AGENTS.md").is_file()
    assert (REPO_ROOT / "pcc").is_dir()


@pytest.mark.intent
def test_every_obligation_has_constraints():
    """Self-completeness guard: each obligation must keep both static locks and
    behavioural coverage, so the suite cannot silently lose an obligation."""
    required = [
        # static locks, one class per obligation + layering
        "TestObligation1ModeLabeling", "TestObligation2PerformanceProven",
        "TestObligation3EcosystemGeneric", "TestObligation4SelfBackendFirstClass",
        "TestObligation5FixedPointContract", "TestObligation6FiveGCComparativeStatic",
        "TestObligation7ValueModelStatic",
        # behavioural coverage
        "TestObligation4SelfBackendBehavioural", "TestPythonSemanticsDifferential",
        "TestObligation7Behavioural", "TestObligation6GCEquality",
        "TestMetamorphicDifferential", "TestOwnedBackendDeterminism",
        "TestObligation3PackageRoundTrip",
        "TestIntentGaps",
        # corpora that must exist
        "SEMANTICS_CASES", "ERROR_MODEL_CASES", "STDLIB_CASES", "IDENTITY_CASES",
        "INT_BIGNUM_CASES", "VALUECLASS_CASES", "GC_PROGRAMS", "GAP_CASES",
        "GC_GAP_PROGRAMS", "CROSS_BACKEND_CASES",
    ]
    g = globals()
    missing = [n for n in required if n not in g]
    assert not missing, f"intent coverage dropped: {missing}"
    for corpus in ("SEMANTICS_CASES", "ERROR_MODEL_CASES", "STDLIB_CASES",
                   "IDENTITY_CASES", "INT_BIGNUM_CASES", "VALUECLASS_CASES",
                   "GC_PROGRAMS", "CROSS_BACKEND_CASES"):
        assert len(g[corpus]) > 0, f"corpus emptied: {corpus}"
    assert len(METAMORPHIC_SEEDS) >= 20 and len(STR_LIST_SEEDS) >= 20


@pytest.mark.intent
class TestObligation1ModeLabeling:
    """host pcc != pcc1 | cpython-compat != pcc-native | libpython != no-libpython
    | LLVM-backed != self-backed | stage1 != pcc1->pcc2->pcc3 fixed point."""

    def test_cli_exposes_the_three_mode_axes(self):
        src = _read("pcc/driver/cli_core.py")
        for flag in ("--backend", "--python-libpython", "--ir-scaffold"):
            assert flag in src, f"{flag} mode axis missing from cli_core.py"

    def test_cli_rejects_unlabeled_mode_values(self):
        src = _read("pcc/driver/cli_core.py")
        assert "invalid --python-libpython" in src
        assert "invalid --ir-scaffold" in src

    def test_bootstrap_baseline_keeps_backend_modes_distinct(self):
        state = json.loads(_read("tests/bootstrap_gate_baseline.json"))["current_state"]
        assert set(state) >= {"llvm", "self"}, "backend modes collapsed"
        for mode in ("llvm", "self"):
            for stage in ("stage1", "stage2", "stage3"):
                assert isinstance(state[mode][stage]["links_libpython"], bool)

    def test_fallback_baseline_is_mode_labeled(self):
        """Fallback counts are tracked per-mode (off vs on) — not one number."""
        fb = json.loads(_read("tests/fallback_baseline.json"))
        assert "totals" in fb and "per_module" in fb
        assert "on_mode_totals" in fb, "on-mode fallback counts not separately labeled"

    def test_mode_boundaries_documented(self):
        agents = _read("AGENTS.md")
        for boundary in ("no-libpython", "self-backed", "cpython-compat", "pcc-native"):
            assert boundary in agents, f"mode boundary undocumented: {boundary}"


@pytest.mark.intent
class TestObligation2PerformanceProven:
    """C-like claims require IR-shape evidence + a slow path preserving semantics."""

    def test_hotpath_ir_shape_evidence_harness_exists(self):
        vc = _read("tests/python/test_py_value_class_unboxed.py")
        ti = _read("tests/python/test_py_typed_int_unboxed.py")
        for name, txt in (("value_class", vc), ("typed_int", ti)):
            assert ("emit_llvm_only" in txt) or ("ir_text" in txt), (
                f"{name} unboxed test no longer asserts over emitted IR text"
            )
        assert "py_instance_new" in vc, (
            "value-class IR-shape test must assert the boxing path is gone"
        )

    def test_runtime_benchmark_harness_exists(self):
        """Performance is proven by a runtime benchmark, not asserted in prose."""
        assert (REPO_ROOT / "tests/python/test_gc_performance.py").is_file()


@pytest.mark.intent
class TestObligation3EcosystemGeneric:
    """No ``if package == "numpy"``; fix the reusable mechanism."""

    _ECO = ("numpy", "pandas", "torch", "scipy", "pyarrow", "sklearn")
    _RE = re.compile(
        r"""==\s*["'](?:%s)["']|["'](?:%s)["']\s*=="""
        % ("|".join(_ECO), "|".join(_ECO))
    )

    def test_no_package_special_casing_in_frontend(self):
        offenders = [
            f"{rel}:{i}"
            for rel, txt in _iter_source_files("pcc/frontends/python", "pcc/driver/project.py")
            for i, line in enumerate(txt.splitlines(), 1)
            if self._RE.search(line)
        ]
        assert not offenders, f"ecosystem package special-cased: {offenders}"

    def test_no_package_special_casing_in_cli_or_codegen(self):
        offenders = [
            f"{rel}:{i}"
            for rel, txt in _iter_source_files("pcc/driver/cli_core.py", "pcc/driver/cli_bootstrap.py", "pcc/frontends/c/codegen")
            for i, line in enumerate(txt.splitlines(), 1)
            if self._RE.search(line)
        ]
        assert not offenders, f"ecosystem package special-cased: {offenders}"

    def test_c_kernel_carries_no_high_level_package_names(self):
        offenders = [
            f"{rel}:{i}"
            for rel, txt in _iter_source_files(
                "pcc/runtime/src", "pcc/runtime/include", suffixes=(".c", ".h")
            )
            for i, line in enumerate(txt.splitlines(), 1)
            if self._RE.search(line)
        ]
        assert not offenders, f"C kernel references ecosystem packages: {offenders}"


@pytest.mark.intent
class TestObligation4SelfBackendFirstClass:
    """No silent fallback to LLVM after --backend=self."""

    def test_self_backend_gate_exists(self):
        gates = list((REPO_ROOT / "tests" / "python").glob("test_self_backend*gate*.py"))
        gates += list((REPO_ROOT / "tests" / "python" / "gc").glob("test_pcc_bootstrap_full_gc*.py"))
        assert gates, "no self-backend execution-root gate found"


@pytest.mark.intent
class TestObligation5FixedPointContract:
    """Differences are classified into 8 categories, not patched around."""

    def test_byte_identity_recorded_for_both_backends(self):
        bi = json.loads(_read("tests/bootstrap_gate_baseline.json"))["byte_identical_pcc2_pcc3"]
        assert bi.get("llvm") is True, "llvm pcc2==pcc3 byte identity not held"
        assert bi.get("self") is True, "self pcc2==pcc3 byte identity not held"

    def test_difference_classification_taxonomy_documented(self):
        agents = _read("AGENTS.md")
        for category in ("semantic", "IR-text", "class-layout", "object-model",
                         "backend nondeterminism", "link metadata", "perf-only",
                         "diagnostic"):
            assert category in agents, f"difference category dropped: {category}"

    def test_difference_classifier_module_exists(self):
        from pcc.bootstrap import diff_classifier

        assert set(diff_classifier.CATEGORIES) == {
            "semantic", "IR-text", "class-layout", "object-model",
            "backend-nondeterminism", "link-metadata", "perf-only", "diagnostic"}
        assert diff_classifier.classify_diff_text("LC_UUID changed").category == (
            "link-metadata"
        )

    def test_stage_chain_named(self):
        agents = _read("AGENTS.md")
        for stage in ("pcc0", "pcc1", "pcc2", "pcc3"):
            assert stage in agents, f"bootstrap stage name dropped: {stage}"


@pytest.mark.intent
class TestObligation6FiveGCComparativeStatic:
    """Five backends; one slot trace/update contract; per-backend gates."""

    _GC_KIND_RE = re.compile(r"PCC_GC_KIND_\w+\s*=\s*(\d+)")

    def test_exactly_five_gc_backends(self):
        header = _read("pcc/runtime/include/py_runtime.h")
        values = sorted(int(m) for m in self._GC_KIND_RE.findall(header))
        assert values == [0, 1, 2, 3, 4], f"5-GC matrix changed shape: {values}"

    def test_all_five_backend_names_present(self):
        header = _read("pcc/runtime/include/py_runtime.h")
        for name in ("REFCOUNT_CYCLE", "INCREMENTAL_TRICOLOR", "CONCURRENT_MARK_SWEEP",
                     "GENERATIONAL_MINOR_MAJOR", "COLORED_RELOCATING"):
            assert f"PCC_GC_KIND_{name}" in header, f"backend slot missing: {name}"

    def test_single_slot_barrier_contract(self):
        header = _read("pcc/runtime/include/py_runtime.h")
        internal = _read("pcc/runtime/src/py_internal.h")
        assert "pcc_gc_load_ptr" in header
        assert "pcc_gc_store_ptr" in header
        assert "pcc_gc_slot_is_runtime_root" in internal
        mapped_roots = _read("pcc/runtime/py/freestanding_gc_mapped_roots.py")
        assert '@c_abi_export("pcc_gc_visit_registered_root_slots")' in mapped_roots

    def test_every_backend_has_its_own_bootstrap_gate(self):
        gc_dir = REPO_ROOT / "tests" / "python" / "gc"
        for n in range(5):
            assert (gc_dir / f"test_pcc_bootstrap_full_gc{n}.py").is_file(), (
                f"GC backend {n} has no dedicated bootstrap gate"
            )

    def test_gc_production_contract_suite_exists(self):
        """Finalizer/weakref/cycle production-contract tests must exist for the
        5-GC equality rule (none may win by weakening these)."""
        assert (REPO_ROOT / "tests/python/gc_production_contract").is_dir()


@pytest.mark.intent
class TestObligation7ValueModelStatic:
    def test_value_model_public_api_is_optin(self):
        init = _read("pcc/__init__.py")
        for name in ("valueclass", "ValueBox", "ValuePayload"):
            assert f'"{name}"' in init, f"value-model export missing: {name}"

    def test_explicit_raw_machine_int_types_exist(self):
        extern = _read("pcc/extern/__init__.py")
        assert "c_int64" in extern and "c_uint64" in extern and '"i64"' in extern

    def test_ordinary_class_identity_regression_home_exists(self):
        candidates = ["tests/python/test_value_model_valhalla.py",
                      "tests/python/test_value_class_runtime.py",
                      "tests/python/test_valueclass_weakref_runtime.py"]
        assert any((REPO_ROOT / c).is_file() for c in candidates)


# =========================================================================== #
# Heavy `integration` constraints — compile / differential / multi-backend.
# =========================================================================== #
@pytest.mark.integration
class TestObligation4SelfBackendBehavioural:
    def test_self_backend_is_a_working_execution_root(self, tmp_path):
        out = _compile_and_run(tmp_path, "def main():\n    print('self-root-ok')\nmain()\n")
        assert out.strip() == "self-root-ok"

    def test_off_mode_never_silently_links_libpython(self, tmp_path):
        exe = _compile(tmp_path, "def main():\n    print('x')\nmain()\n", expect_ok=True)
        assert not _links_libpython(exe), "off-mode artifact silently linked libpython"


@pytest.mark.integration
class TestObligation3PackageRoundTrip:
    """Generic ecosystem support: package mechanisms, not package-name branches."""

    def test_generic_pure_python_package_install_import_execute(self, tmp_path):
        from pcc.package.install import install_package

        project = tmp_path / "vector_pkg-0.1"
        pkg = project / "vector_pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "from vector_pkg.core import zeros\n",
            encoding="utf-8",
        )
        (pkg / "core.py").write_text(
            "class Array:\n"
            "    def __init__(self, values):\n"
            "        self.values = values\n"
            "    def sum(self):\n"
            "        total = 0.0\n"
            "        for value in self.values:\n"
            "            total += value\n"
            "        return total\n"
            "def zeros(n):\n"
            "    values = []\n"
            "    for _ in range(n):\n"
            "        values.append(0.0)\n"
            "    return Array(values)\n",
            encoding="utf-8",
        )
        site = tmp_path / "site"
        result = install_package(
            str(project),
            target_dir=site,
            cache_dir=tmp_path / "cache",
        )
        assert result["ok"] is True

        prog = "import vector_pkg as np\nprint(np.zeros(3).sum())\n"
        out = _compile_and_run(
            tmp_path / "roundtrip",
            prog,
            env_extra={"PCC_PACKAGE_SITE": str(site)},
        )
        assert out.strip() == "0.0"

    def test_cpython_extension_abi_boundary_is_explicit(self, tmp_path):
        site = tmp_path / "site"
        pkg = site / "bad_ext_pkg"
        pkg.mkdir(parents=True)
        (pkg / "pcc-package.json").write_text("{}", encoding="utf-8")
        (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        (pkg / "_native.cpython-314-darwin.so").write_bytes(b"libpcc_runtime")

        proc = _compile_result(
            tmp_path / "abi_boundary",
            "import bad_ext_pkg\nprint('never')\n",
            env_extra={"PCC_PACKAGE_SITE": str(site)},
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, "CPython extension ABI was accepted in pcc-native mode"
        assert "PCC-PKG-004" in combined
        assert "installed package cannot be used by pcc-native no-libpython import" in combined


@pytest.mark.integration
class TestPythonSemanticsDifferential:
    """Conformance corpus: pcc (--backend self --python-libpython=off) must match
    the CPython oracle, idiom by idiom. This is the differential/oracle testing
    that CPython's own suite and csmith/EMI embody."""

    @pytest.mark.parametrize("source", [s for _, s in SEMANTICS_CASES],
                             ids=[i for i, _ in SEMANTICS_CASES])
    def test_matches_cpython(self, tmp_path, source):
        assert _compile_and_run(tmp_path, source) == _run_cpython(tmp_path, source)

    @pytest.mark.parametrize("source", [s for _, s in ERROR_MODEL_CASES],
                             ids=[i for i, _ in ERROR_MODEL_CASES])
    def test_error_model_matches_cpython(self, tmp_path, source):
        """Error-model fidelity: pcc raises/handles the same exceptions as
        CPython (claims-Python-semantics must hold for the failure path too)."""
        assert _compile_and_run(tmp_path, source) == _run_cpython(tmp_path, source)

    @pytest.mark.parametrize("source", [s for _, s in STDLIB_CASES],
                             ids=[i for i, _ in STDLIB_CASES])
    def test_stdlib_breadth_matches_cpython(self, tmp_path, source):
        assert _compile_and_run(tmp_path, source) == _run_cpython(tmp_path, source)


@pytest.mark.integration
class TestMetamorphicDifferential:
    """csmith/EMI-style: deterministically generate random (proven-green) nested
    programs and diff pcc against CPython. Coverage scales with combinatorial
    depth; a regression in arithmetic/comparison/conditional codegen turns a
    seed red. Reproduce any failure with _gen_program(<seed>)."""

    @pytest.mark.parametrize("seed", METAMORPHIC_SEEDS)
    def test_random_program_matches_cpython(self, tmp_path, seed):
        src = _gen_program(seed)
        assert _compile_and_run(tmp_path, src) == _run_cpython(tmp_path, src), src

    @pytest.mark.parametrize("seed", STR_LIST_SEEDS)
    def test_random_str_list_program_matches_cpython(self, tmp_path, seed):
        src = _gen_str_list_program(seed)
        assert _compile_and_run(tmp_path, src) == _run_cpython(tmp_path, src), src


@pytest.mark.integration
class TestOwnedBackendDeterminism:
    """Independent owned compilations must agree with each other and CPython."""

    @pytest.mark.parametrize("source", [s for _, s in CROSS_BACKEND_CASES],
                             ids=[i for i, _ in CROSS_BACKEND_CASES])
    def test_independent_self_compilations_agree_with_cpython(self, tmp_path, source):
        cpy = _run_cpython(tmp_path, source)
        first = _compile_and_run(tmp_path / "first", source, backend="self")
        second = _compile_and_run(tmp_path / "second", source, backend="self")
        assert first == cpy, "first self compilation diverges from CPython"
        assert second == cpy, "second self compilation diverges from CPython"
        assert first == second, "independent self compilations disagree"


@pytest.mark.integration
class TestObligation7Behavioural:
    @pytest.mark.parametrize("source", [s for _, s in IDENTITY_CASES],
                             ids=[i for i, _ in IDENTITY_CASES])
    def test_ordinary_class_identity_matches_cpython(self, tmp_path, source):
        """Ordinary classes keep identity (id/is/weakref/__dict__/mutation/
        subclass/finalizer) — value classes never steal it."""
        assert _compile_and_run(tmp_path, source) == _run_cpython(tmp_path, source)

    @pytest.mark.parametrize("source", [s for _, s in INT_BIGNUM_CASES],
                             ids=[i for i, _ in INT_BIGNUM_CASES])
    def test_int_is_arbitrary_precision(self, tmp_path, source):
        """int is an arbitrary-precision SEMANTIC type; value-lane overflow
        promotes to bignum, never wraps."""
        assert _compile_and_run(tmp_path, source) == _run_cpython(tmp_path, source)

    def test_valueclass_id_is_diagnosed_as_identity_escape(self, tmp_path):
        """Value classes are identity-FREE: id() on a payload must be a
        compile-time identity-escape diagnostic, not a fabricated id."""
        prog = ("import pcc\n@pcc.valueclass\nclass P:\n    x: int\n    y: int\n"
                "def main():\n    p = P(1, 2)\n    print(id(p))\nmain()\n")
        proc = _compile_result(tmp_path, prog)
        assert proc.returncode != 0, "id() on a valueclass payload was not rejected"
        assert "id()" in proc.stderr and "valueclass" in proc.stderr, proc.stderr

    @pytest.mark.parametrize("source,expected", [(s, e) for _, s, e in VALUECLASS_CASES],
                             ids=[i for i, _, _ in VALUECLASS_CASES])
    def test_value_class_value_semantics(self, tmp_path, source, expected):
        """Value classes are opt-in identity-free payloads with value equality,
        field access, and nesting. (Golden expected — the Python semantics.)"""
        assert _compile_and_run(tmp_path, source) == expected

    def test_valueclass_method_field_access(self, tmp_path):
        assert _compile_and_run(tmp_path, VALUECLASS_GAP_SRC).strip() == "25"

    def test_valueclass_genexpr_field_access(self, tmp_path):
        assert _compile_and_run(tmp_path, VALUECLASS_GENEXPR_GAP_SRC).strip() == "9"


@pytest.mark.integration
class TestObligation6GCEquality:
    """5-GC Production Equality Rule: the same GC-semantics program produces
    identical observable output under PCC_GC_BACKEND=0..4. Multi-collector
    torture (Go gccheckmark / HotSpot GC modes)."""

    @pytest.mark.parametrize("source", [s for _, s in GC_PROGRAMS],
                             ids=[i for i, _ in GC_PROGRAMS])
    def test_identical_across_backends(self, tmp_path, source):
        outs = {n: _compile_and_run(tmp_path / f"gc{n}", source, gc_backend=n)
                for n in range(5)}
        diverged = {n: o for n, o in outs.items() if o != outs[0]}
        assert not diverged, f"GC backends diverge from backend-0: {diverged}"

    if GC_GAP_PROGRAMS:

        @pytest.mark.parametrize("source", GC_GAP_PROGRAMS)
        def test_identical_across_backends_gap(self, tmp_path, source):
            """GC-semantics surfaces that do not yet agree across backends."""
            outs = {
                n: _compile_and_run(tmp_path / f"gc{n}", source, gc_backend=n)
                for n in range(5)
            }
            diverged = {n: o for n, o in outs.items() if o != outs[0]}
            assert not diverged, f"GC backends diverge from backend-0: {diverged}"


# =========================================================================== #
# INTENT GAPS — constraints the implementation does NOT yet satisfy (red).
# Each flips to XPASS when the obligation is met. Mislabeling guard: every
# verified-red case was reproduced first; intents that
# turned out implemented (first-class fns in containers, lambdas, map/filter over
# user fns, resurrection equality, int-overflow promotion) are green above.
# =========================================================================== #
@pytest.mark.integration
class TestIntentGaps:
    """Verified-red intent gaps, when any remain."""

    if GAP_CASES:

        @pytest.mark.parametrize("source", GAP_CASES)
        def test_unmet_obligation(self, tmp_path, source):
            """Each passes only when pcc matches CPython."""
            assert _compile_and_run(tmp_path, source) == _run_cpython(tmp_path, source)
