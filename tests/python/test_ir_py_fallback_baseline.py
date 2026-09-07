"""Phase 9 ratchet: pcc/llvm_capi/ir.py py_cpy_* count is monotonically
decreasing.

This is the gate test for Issue 1 closure. ir.py must reach 0 py_cpy_*
in ON mode for the bootstrap binary to be link-clean (no libpython).

Each Phase 9 sub-task should DECREASE the numbers in
``ir_py_baseline.json``. No commit may increase them. When a fix lands,
re-capture the baseline (smaller numbers) and re-run pytest.
"""
from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BASELINE_JSON = _REPO_ROOT / "tests" / "ir_py_baseline.json"
_IR_PY = _REPO_ROOT / "pcc" / "llvm_capi" / "ir.py"


def _load_baseline() -> dict:
    with open(_BASELINE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def ir_py_on_text() -> str:
    """Compile ir.py in ON mode once for closure and hot-shape gates."""
    from pcc.py_frontend.pipeline import compile_python

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "ir.ll"
        compile_python(
            str(_IR_PY), str(out),
            emit_llvm_only=True,
            ir_scaffold_mode="on",
        )
        return out.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ir_py_on_counts(ir_py_on_text: str) -> dict:
    """Return per-symbol py_cpy_* counts from the shared compiled IR."""
    # Per-symbol call counts: only CALL instructions (excludes declare/define
    # lines so we don't over-count by the number of extern declarations).
    call_pattern = re.compile(r"\bcall [^\n]*@(py_cpy_[a-z0-9_]+)")
    by_sym = dict(Counter(call_pattern.findall(ir_py_on_text)))
    n_total = sum(by_sym.values())
    by_sym["_total"] = n_total
    return by_sym


@pytest.fixture(scope="module")
def ir_py_contextual_text(tmp_path_factory) -> str:
    """Compile ir.py with the closed-world exports used by the real pcc1."""
    from pcc.py_frontend import pipeline

    entry = str(_REPO_ROOT / "pcc" / "__main__.py")
    srcs, mods = pipeline._collect_relative_module_closure(
        entry,
        include_same_package_absolute=True,
        recurse_same_package_absolute=True,
    )
    srcs, mods = pipeline._filter_ir_scaffold_closure(
        srcs,
        mods,
        ir_scaffold_mode="on",
    )
    seen = {module_name: source for source, module_name in zip(srcs, mods)}
    pipeline._expand_native_extension_module_object_ports(srcs, mods, seen)
    srcs, mods = pipeline._prepare_multi_source_compile_closure(
        srcs,
        mods,
        recursive_stdlib=True,
        ir_scaffold_mode="on",
    )
    output = tmp_path_factory.mktemp("ir-contextual")
    counts = pipeline.compile_contextual_per_module_fallback_counts(
        srcs,
        mods,
        {"pcc.llvm_capi.ir"},
        ir_scaffold_mode="on",
        strict_no_libpython=True,
        emit_ir_dir=str(output),
    )
    assert counts == {"pcc.llvm_capi.ir": 0}
    return (output / "pcc_llvm_capi_ir.ll").read_text(encoding="utf-8")


def _function_body(ir_text: str, suffix: str) -> str:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    match = pattern.search(ir_text)
    assert match is not None, "missing compiled function: " + suffix
    return match.group(1)


@pytest.mark.parametrize(
    ("arity", "managed_params"),
    [
        (0, ("builder", "fn")),
        (1, ("builder", "fn", "arg0")),
        (2, ("builder", "fn", "arg0", "arg1")),
    ],
)
def test_small_arity_call_wrapper_keeps_generic_native_root_contract(
    ir_py_on_text: str,
    arity: int,
    managed_params: tuple[str, ...],
) -> None:
    body = _function_body(ir_py_on_text, "IRBuilder_call" + str(arity))

    # The fixed-cache-hit proposal was measured and denied: it raised median
    # instructions/cycles/RSS and missed the wall threshold.  Keep one direct
    # call to the mutation-safe generic core; do not retain the rejected
    # fast/slow helper frames in the native closure.
    generic_symbol = (
        "@user_pcc_llvm_capi_ir__irbuilder_call_from_args_list("
    )
    assert body.count(generic_symbol) == 1
    assert "_irbuilder_call" + str(arity) + "_fast" not in ir_py_on_text
    assert "_irbuilder_call" + str(arity) + "_slow" not in ir_py_on_text
    assert body.count("@pcc_gc_frame_enter(") == len(managed_params)

    # GC3/4 may relocate every managed argument during str/concat work.  Each
    # raw ABI value must be stored once into an updateable borrowed root, and
    # all later uses must reload from that slot rather than reuse the raw SSA.
    for param in managed_params:
        raw_uses = re.findall(
            r"(?<![A-Za-z0-9_.])%" + re.escape(param) + r"(?=[,)])",
            body,
        )
        assert len(raw_uses) == 1, (param, raw_uses)
        assert re.search(
            r"store ptr %"
            + re.escape(param)
            + r", ptr %"
            + re.escape(param)
            + r"\.addr\.",
            body,
        )
        assert re.search(
            r"@pcc_gc_load_borrowed_ptr\([^\n]*%"
            + re.escape(param)
            + r"\.gc\.slot\.",
            body,
        )


def test_generic_call_argument_rendering_avoids_three_item_container(
    ir_py_on_text: str,
) -> None:
    body = _function_body(
        ir_py_on_text,
        "_irbuilder_call_from_args_list",
    )

    # Each argument used to allocate [type_text, " ", value_ref] and call
    # _join_text.  Besides the allocation cost, that native return path could
    # lose the owned result before arg_parts retained it.  Named concatenation
    # locals keep the moving-GC roots explicit without a per-argument list.
    assert "@py_list_new(i64 3)" not in body


def test_declared_signature_uses_static_fields_without_memoization(
    ir_py_contextual_text: str,
) -> None:
    init_body = _function_body(ir_py_contextual_text, "Function___init__")
    exact_body = _function_body(ir_py_contextual_text, "_is_exact_function")
    call_body = _function_body(
        ir_py_contextual_text,
        "_irbuilder_call_from_args_list",
    )

    # The Function-owned signature cache was measured and denied.  Preserve
    # the established current 0..24 layout with no cache slot or setter, while keeping
    # exact Functions statically narrowed so their established ftype/name
    # fields use constant-index loads instead of dynamic getattr.
    assert re.search(
        r"@py_instance_set_field\([^\n]*i32 24, ptr @\.pystr\.obj\.",
        init_body,
    )
    assert "i32 25" not in init_body
    assert "_callee_signature_cache" not in ir_py_contextual_text
    assert re.search(
        r"%self\.ftype\.[^\n]*@py_instance_get_field\([^\n]*i32 10\)",
        call_body,
    )
    assert re.search(
        r"%self\.name\.[^\n]*@py_instance_get_field\([^\n]*i32 12\)",
        call_body,
    )
    assert call_body.index("%self.ftype.") < call_body.index("%attr.ftype.")
    assert call_body.index("%self.name.") < call_body.index("%attr.name.")

    # The exact-type guard keeps Function subclasses on the dynamic branch,
    # where descriptor/__getattribute__ overrides remain observable.  The
    # signature args field itself may be replaced with a list by supported
    # mutation, so even the exact branch must use the generic iterator ABI.
    assert call_body.count(
        "@user_pcc_llvm_capi_ir__is_exact_function("
    ) == 1
    assert re.search(
        r"@py_obj_getattr\([^\n]*@\.pyattr\.__class__",
        exact_body,
    )
    assert re.search(
        r"load ptr, ptr @\.class\.pcc_llvm_capi_ir\.Function",
        exact_body,
    )
    assert "icmp eq i64" in exact_body
    assert re.search(r"@pcc_gc_release\(ptr %type\.", exact_body)
    assert exact_body.count("@pcc_gc_frame_enter(") == 1
    assert len(re.findall(r"(?<![A-Za-z0-9_.])%value(?=[,)])", exact_body)) == 1
    assert re.search(r"store ptr %value, ptr %value\.addr\.", exact_body)
    assert re.search(
        r"@pcc_gc_load_borrowed_ptr\([^\n]*%value\.gc\.slot\.",
        exact_body,
    )
    args_load = re.search(
        r"(?P<value>%self\.args\.[\w.]+) = [^\n]*@py_instance_get_field\([^\n]*i32 1\)",
        call_body,
    )
    assert args_load is not None
    # The owned field result is now rooted across iterator construction.
    # Check its data flow, rather than a three-line adjacency that excluded
    # the required root setup, and require its separate owner to be consumed.
    args_value = re.escape(args_load.group("value"))
    after_load = call_body[args_load.end():]
    iter_use = re.search(r"@py_obj_iter\(ptr " + args_value + r"\)", after_load)
    assert iter_use is not None
    assert re.search(
        r"@pcc_gc_release\(ptr " + args_value + r"\)",
        after_load[iter_use.end():],
    )

    # The typed ftype load is owned and must be rooted/released on all exits.
    # The raw Function ABI value itself is stored once and reloaded from its
    # updateable borrowed root across allocating string work.
    assert "%current_ftype.owned." in call_body
    assert call_body.count(
        "@pcc_gc_release(ptr %current_ftype.release.current."
    ) >= 2
    assert len(re.findall(r"store ptr %fn, ptr %fn\.addr\.", call_body)) == 1
    assert re.search(
        r"@pcc_gc_load_borrowed_ptr\([^\n]*%fn\.gc\.slot\.",
        call_body,
    )
    assert "%exact_fn.owned." not in call_body
    assert re.search(
        r"store ptr %fn\.[0-9.]+, ptr %exact_fn\.addr\.",
        call_body,
    )
    assert re.search(
        r"@pcc_gc_load_borrowed_ptr\([^\n]*%exact_fn\.gc\.slot\.",
        call_body,
    )


def test_ir_py_total_does_not_regress(ir_py_on_counts):
    """Aggregate count must not exceed baseline. Path A's success
    requires this number to monotonically reach 0."""
    baseline = _load_baseline()
    expected_total = baseline["_total"]
    actual_total = ir_py_on_counts["_total"]
    assert actual_total <= expected_total, (
        f"ir.py py_cpy_* total regressed: {actual_total} > "
        f"{expected_total}. If this is intentional progress (a "
        f"REDUCTION), recapture baseline JSON. If it's an unintended "
        f"regression, find the new dynamic-Python idiom that crept in."
    )


def test_ir_py_per_symbol_does_not_regress(ir_py_on_counts):
    """No single ``py_cpy_*`` symbol's count grows past its baseline.
    Per-symbol granularity catches localised regressions: e.g. a fix
    that drops total by 10 but increases ``py_cpy_call1`` by 2 still
    fails this test (something else got worse during the fix).

    Symbols that didn't appear in the baseline must stay at 0 (i.e.
    no NEW dynamic-Python symbol may surface).
    """
    baseline = _load_baseline()
    failures: list[str] = []

    for sym, baseline_count in baseline.items():
        if sym.startswith("_"):
            continue
        actual = ir_py_on_counts.get(sym, 0)
        if actual > baseline_count:
            failures.append(
                f"{sym}: {actual} > baseline {baseline_count}"
            )

    for sym, actual in ir_py_on_counts.items():
        if sym.startswith("_"):
            continue
        if sym in baseline:
            continue
        if actual > 0:
            failures.append(
                f"{sym}: {actual} (NEW symbol — was 0 in baseline)"
            )

    assert not failures, (
        "ir.py per-symbol regressions:\n  "
        + "\n  ".join(failures)
        + "\n(if intentional reduction, recapture baseline)"
    )


def test_ir_py_baseline_self_consistent():
    """Sanity: the JSON's _total field equals the sum of per-symbol
    counts. Catches editing typos in the baseline."""
    baseline = _load_baseline()
    expected = baseline["_total"]
    actual = sum(
        v for k, v in baseline.items()
        if not k.startswith("_") and isinstance(v, int)
    )
    assert actual == expected, (
        f"baseline JSON inconsistent: _total={expected} but "
        f"per-symbol sum={actual}"
    )
