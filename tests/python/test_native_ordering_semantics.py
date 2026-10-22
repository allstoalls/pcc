"""Native ordering comparisons follow CPython's rich-comparison rules.

The runtime answered every unorderable pair (int < str, None < None,
complex, dict, set < list, instances without ordering methods or whose
methods return NotImplemented) through a three-way fallback that called
them equal, so ``<`` was False and ``<=`` True instead of a TypeError, and
sorted()/min()/max()/list.sort() returned wrong results. Tuples and lists
compared items three-way instead of finding the first unequal item with
``==`` and applying the operator to it, and str/tuple subclasses compared
as unknown instances.
"""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import textwrap


PROGRAM = textwrap.dedent(
    """
    class Plain:
        pass


    class Declines:
        def __lt__(self, other):
            return NotImplemented

        def __gt__(self, other):
            return NotImplemented


    class Ordered:
        def __init__(self, value):
            self.value = value

        def __lt__(self, other):
            if not isinstance(other, Ordered):
                return NotImplemented
            return self.value < other.value


    class MyStr(str):
        pass


    class MyTuple(tuple):
        pass


    class DeclinesStr(str):
        def __lt__(self, other):
            return NotImplemented

        def __le__(self, other):
            return NotImplemented

        def __gt__(self, other):
            return NotImplemented

        def __ge__(self, other):
            return NotImplemented


    class DeclinesTuple(tuple):
        def __lt__(self, other):
            return NotImplemented

        def __le__(self, other):
            return NotImplemented

        def __gt__(self, other):
            return NotImplemented

        def __ge__(self, other):
            return NotImplemented


    class LeftStr(str):
        def __lt__(self, other):
            print("left-str-declined")
            return NotImplemented


    class PriorityStr(str):
        def __lt__(self, other):
            print("base-str-lt")
            return False


    class ReflectedStr(PriorityStr):
        def __gt__(self, other):
            print("subtype-str-gt")
            return True


    def attempt(label, fn):
        try:
            print(label, fn())
        except TypeError as exc:
            print(label, "TypeError", exc)


    def all_orderings(label, left, right):
        attempt(label + "<", lambda: left < right)
        attempt(label + "<=", lambda: left <= right)
        attempt(label + ">", lambda: left > right)
        attempt(label + ">=", lambda: left >= right)


    def typed_str_order(left: DeclinesStr, right: DeclinesStr) -> bool:
        return left < right


    def typed_tuple_order(left: DeclinesTuple, right: DeclinesTuple) -> bool:
        return left >= right


    def main():
        values = [1, "a", None, 2.5, (1, "a"), (1, 2), [1, 2], [1, "a"], b"a", 1j,
                  {1}, {"k": 1}, float("nan"), bytearray(b"b"), memoryview(b"c")]
        (one, text, none, real, tup_a, tup_b, list_b, list_a, raw, cplx, aset,
         adict, nan, buf, view) = values
        attempt("int<str", lambda: one < text)
        attempt("str>=int", lambda: text >= one)
        attempt("None<None", lambda: none < none)
        attempt("None<=None", lambda: none <= none)
        attempt("bytes<str", lambda: raw < text)
        attempt("complex<complex", lambda: cplx < cplx)
        attempt("set<list", lambda: aset < list_b)
        attempt("dict<dict", lambda: adict < adict)
        attempt("view<view", lambda: view < view)
        attempt("bytes<view", lambda: raw < view)
        attempt("float<int", lambda: real < one)
        attempt("int<=float", lambda: one <= real)
        attempt("nan", lambda: (nan < 1, nan >= 1, 1 > nan, nan <= nan))
        attempt("bytes<bytearray", lambda: raw < buf)
        attempt("bytearray<view", lambda: buf < view)
        attempt("set<=set", lambda: (aset <= {1, 2}, aset < aset, {1, 2} > aset))
        attempt("tuple<tuple", lambda: tup_a < tup_b)
        attempt("list<list", lambda: list_b < list_a)
        attempt("prefix", lambda: ((1, 2) < (1, 2, 0), [1] >= [1], (2,) > (1, 9)))
        attempt("same-nan", lambda: ((nan,) < (nan,), (nan,) <= (nan,)))
        attempt("tuple-user", lambda: (Ordered(1), 1) < (Ordered(1), 2))
        attempt("plain<plain", lambda: Plain() < Plain())
        attempt("declines<declines", lambda: Declines() < Declines())
        attempt("declines>=1", lambda: Declines() >= one)
        attempt("ordered<ordered", lambda: Ordered(1) < Ordered(2))
        attempt("reflected>", lambda: Ordered(2) > Ordered(1))
        attempt("reflected<=", lambda: Ordered(2) <= Ordered(1))
        attempt("ordered<int", lambda: Ordered(1) < one)
        attempt("mystr", lambda: (MyStr("a") < MyStr("b"), MyStr("b") >= "a", "a" < MyStr("b")))
        attempt("mytuple", lambda: MyTuple((1, 2)) < MyTuple((1, 3)))
        # A builtin-base payload is not a third candidate after both selected
        # overriding methods explicitly declined, including identical operands.
        ds_a, ds_b = DeclinesStr("a"), DeclinesStr("b")
        dt_a, dt_b = DeclinesTuple((1,)), DeclinesTuple((2,))
        all_orderings("declined-str", ds_a, ds_b)
        all_orderings("declined-tuple", dt_a, dt_b)
        all_orderings("same-declined-str", ds_a, ds_a)
        all_orderings("same-declined-tuple", dt_a, dt_a)
        attempt("typed-declined-str", lambda: typed_str_order(ds_a, ds_b))
        attempt("typed-declined-tuple", lambda: typed_tuple_order(dt_a, dt_b))
        # Inherited and one-sided builtin operations must still answer.
        all_orderings("inherited-str", MyStr("a"), MyStr("b"))
        all_orderings("inherited-tuple", MyTuple((1,)), MyTuple((2,)))
        all_orderings("str-left-declines", ds_a, "b")
        all_orderings("str-right-declines", "a", ds_b)
        all_orderings("tuple-left-declines", dt_a, (2,))
        all_orderings("tuple-right-declines", (1,), dt_b)
        attempt("str-missing-reflected", lambda: LeftStr("a") < MyStr("b"))
        attempt("str-subtype-priority", lambda: PriorityStr("a") < ReflectedStr("b"))
        attempt("subclass-equality", lambda: (ds_a == DeclinesStr("a"), dt_a == DeclinesTuple((1,))))
        attempt("sorted", lambda: sorted([one, text]))
        attempt("sorted-ok", lambda: sorted([3, 1.5, 2, True]))
        attempt("sorted-user", lambda: [o.value for o in sorted([Ordered(3), Ordered(1), Ordered(2)])])
        attempt("min", lambda: min([one, text]))
        attempt("max", lambda: max([text, one]))
        attempt("min-ok", lambda: (min([3, 1.5, 2]), max(["b", "c", "a"])))
        items = [3, none, 1]
        attempt("list.sort", lambda: items.sort())
        # A failed sort leaves every item in the list.
        print("after", len(items), len([x for x in items if x is none]),
              sorted(x for x in items if x is not none))
        numbers = [3, 1, 2]
        numbers.sort()
        print("sorted list", numbers)


    main()
    """
).lstrip()

COLLECTOR_PROBE = textwrap.dedent(
    """
    from pcc.extern import extern, c_int64

    _ordering_gc_backend = extern("pcc_gc_backend", (), c_int64)

    def _ordering_collector_witness():
        print("PCC_ORDERING_GC", _ordering_gc_backend())

    _ordering_collector_witness()
    """
).lstrip()
NATIVE_PROGRAM = COLLECTOR_PROBE + "\n" + PROGRAM
SOURCE_INPUTS = (
    "pcc/runtime/py/py_class.py",
    "pcc/runtime/py/py_protocol_runtime.py",
    "pcc/runtime/py/py_obj_ops_compare.py",
    "tests/python/test_native_ordering_semantics.py",
)


def _source_fingerprints():
    root = Path(__file__).resolve().parents[2]
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in SOURCE_INPUTS
    }


def _verify_collector_output(stdout, backend, reference_stdout):
    # The only intentional native/oracle difference is this first diagnostic
    # line. Compare the complete remaining semantic output.
    line, separator, semantic_output = stdout.partition("\n")
    assert separator and line == f"PCC_ORDERING_GC {backend}", line
    assert semantic_output == reference_stdout
    return int(line.removeprefix("PCC_ORDERING_GC "))


def test_native_ordering_matches_cpython(tmp_path):
    from scripts.ci_worker_size_prior_gate import runtime_identity, sha

    selected = os.environ.get("PCC_RUNTIME_ARCHIVE")
    assert selected, "Set PCC_RUNTIME_ARCHIVE to an admitted source-matched runtime"
    archive = Path(selected)
    assert archive.is_absolute(), "The admitted runtime path must be absolute"
    # One multi-collector archive selects its backend at process startup.
    # Strict admission verifies source/codegen, member inventory and settings.
    runtime = runtime_identity(archive)
    inputs = _source_fingerprints()
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ordering.py"
    oracle_src = tmp_path / "ordering-oracle.py"
    exe = tmp_path / "ordering.out"
    src.write_text(NATIVE_PROGRAM, encoding="utf-8")
    oracle_src.write_text(PROGRAM, encoding="utf-8")
    record = {
        "schema": "pcc.native-ordering.v1", "status": "RUNNING",
        "compiler_mode": "host-pcc0", "backend": "self", "libpython": "off",
        "runtime": runtime, "source_inputs": inputs,
        "native_source_sha256": sha(src), "oracle_source_sha256": sha(oracle_src),
        "probe_sha256": hashlib.sha256(COLLECTOR_PROBE.encode()).hexdigest(),
        "collector_evidence": "pcc_gc_backend() observes backend selection, not collection or movement",
        "executions": [],
    }
    receipt = tmp_path / "native-ordering.json"

    def save():
        receipt.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    save()
    python3 = shutil.which("python3")
    assert python3 is not None, "python3 oracle not found on PATH"
    try:
        ref = subprocess.run([python3, str(oracle_src)], capture_output=True, text=True, timeout=30)
        record["reference"] = {
            "command": [python3, str(oracle_src)], "returncode": ref.returncode,
            "stdout": ref.stdout, "stderr": ref.stderr,
        }
        (tmp_path / "reference.stdout").write_text(ref.stdout, encoding="utf-8")
        (tmp_path / "reference.stderr").write_text(ref.stderr, encoding="utf-8")
        assert ref.returncode == 0 and ref.stderr == "", ref.stderr
        compile_python(
            str(src), str(exe), backend="self", ir_scaffold_mode="on",
            libpython_mode="off", runtime_archive=str(archive),
        )
        record["binary_sha256"] = sha(exe)
        for backend in range(5):
            # Revalidate the exact archive/provenance/inventory for EACH launch;
            # neither a request nor a GC0-only artifact counts as five backends.
            before = runtime_identity(archive)
            assert before == runtime
            assert _source_fingerprints() == inputs
            env = dict(
                os.environ, PCC_GC_BACKEND=str(backend),
                PCC_TEST_NO_NATIVE_PROVISIONING="1", PCC_NO_AUTO_PCC1="1",
            )
            native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, env=env)
            after = runtime_identity(archive)
            row = {
                "requested_gc_backend": backend, "runtime_before": before,
                "runtime_after": after, "command": [str(exe)],
                "returncode": native.returncode, "stdout": native.stdout,
                "stderr": native.stderr,
            }
            record["executions"].append(row)
            (tmp_path / f"gc{backend}.stdout").write_text(native.stdout, encoding="utf-8")
            (tmp_path / f"gc{backend}.stderr").write_text(native.stderr, encoding="utf-8")
            save()
            assert after == runtime
            assert sha(exe) == record["binary_sha256"]
            assert _source_fingerprints() == inputs
            assert native.returncode == 0 and native.stderr == "", f"GC{backend}: {native.stderr}"
            row["observed_gc_backend"] = _verify_collector_output(native.stdout, backend, ref.stdout)
            save()
        record["status"] = "PASS"
    except BaseException as error:
        record.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        save()
