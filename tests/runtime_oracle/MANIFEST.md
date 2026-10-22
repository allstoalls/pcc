# Runtime Oracle Corpus

This directory holds the smallest curated Python programs that exercise
each `pcc/runtime/src/*.c` module's public surface. The **differential
oracle harness** (Phase 0, task #176) runs each program through three
runtime-source paths:

| Path | Source of `libpy_runtime.a` | Role |
|---|---|---|
| `cc-C` | host `cc` compiles `py_runtime/src/*.c` | baseline (trusted) |
| `pcc-C` | `pcc --emit-obj` on `py_runtime/src/*.c` | middle oracle |
| `pcc-Py` | `pcc` on `py_runtime/py/*.py` | ultimate state |

The harness asserts byte-equivalent stdout/stderr/exit for every program
across every source column. That equivalence is the only proof that
Phase 4's pcc-Python runtime is semantically correct.

## Corpus

| Program | Exercises | Runtime modules |
|---|---|---|
| `int_basics.py` | integer arithmetic, conversion, compare | `py_int`, `py_obj_ops` |
| `str_basics.py` | slicing, concat, split, join, case | `py_str` |
| `list_basics.py` | append/pop/insert/reverse/sort, comprehension | `py_list` |
| `tuple_basics.py` | construction, unpack, concat, iteration | `py_tuple` |
| `dict_basics.py` | get/set/del, keys/values/items, containment | `py_dict` |
| `set_basics.py` | add/discard, union/intersection/difference | `py_set` |
| `class_basics.py` | __init__, methods, inheritance, isinstance | `py_class`, `py_obj_ops` |
| `exc_basics.py` | raise/catch/finally, exception hierarchy | `py_exc` |
| `print_basics.py` | print sep/end/file, mixed types | `py_print` |
| `obj_ops_basics.py` | == / is, hash, bool, len, min/max | `py_obj_ops` |
| `os_basics.py` | sys.argv, os.path.isdir, os.path.basename | `py_os` |

## Adding a program

- deterministic output (no timestamps, no randomness, no floats unless
  carefully formatted)
- self-contained (no network, no unstable filesystem assumptions)
- small (<40 lines); the point is coverage shape, not thoroughness
- when adding a program that exercises a new module, update the table
  above and the per-module manifest in the harness

## Relationship to audit

This corpus is **tighter** than the self-host audit's file list. The
audit asks "can pcc produce a valid object for X"; this corpus asks
"does pcc produce the SAME behavior X did before". Those are different
claims — the audit is a codegen capability claim, the oracle is a
semantic-equivalence claim.
