"""globals() publishes its retained namespace owner into a caller root.

Host emitter models and IR checks are distinct from native GC qualification.
"""
from __future__ import annotations

import ast
import copy
import gc
from pathlib import Path
import re
from types import MethodType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _host_branch():
    path = ROOT / "pcc/frontends/python/codegen/call_expression_lowering.py"
    module = ast.parse(path.read_text())
    function = next(node for node in ast.walk(module)
                    if isinstance(node, ast.FunctionDef) and node.name == "_emit_call")
    branch = next(node for node in function.body if isinstance(node, ast.If)
                  and any(isinstance(item, ast.Compare) and isinstance(item.left, ast.Name)
                          and item.left.id == "name"
                          and any(isinstance(value, ast.Constant) and value.value == "globals"
                                  for value in item.comparators)
                          for item in ast.walk(node.test)))
    wrapper = ast.parse("def emit(self, expr):\n    name = 'globals'\n").body[0]
    wrapper.body.append(copy.deepcopy(branch))
    helper_module = ast.parse((ROOT / "pcc/frontends/python/codegen/iterator_builtin_lowering.py").read_text())
    helper = next(node for node in ast.walk(helper_module)
                  if isinstance(node, ast.FunctionDef) and node.name == "_iterator_builtin_is_shadowed")
    namespace = {}
    tree = ast.fix_missing_locations(ast.Module(body=[wrapper, copy.deepcopy(helper)], type_ignores=[]))
    exec(compile(tree, "real_globals_emitter_branch", "exec"), namespace)
    return namespace["emit"], namespace["_iterator_builtin_is_shadowed"]


class _Host:
    def __init__(self, sink=None, pending=False, null=False):
        self.env = {}
        self._module_globals = {}
        self.functions = {}
        self.class_lowering = SimpleNamespace(classes={})
        self._native_builtin_value_aliases = {}
        self._try_err_block = "outer-error"
        self._cpy_operand_cleanup_block = "outer-cpy"
        self.sink = sink
        self.pending = pending
        self.value = None if null else object()
        self.operations = []
        self.slots = {}
        self.builder = SimpleNamespace(load=self._load)
        self._iterator_builtin_is_shadowed = MethodType(_host_branch()[1], self)
    def _current_try_err_block(self):
        return self._try_err_block
    def _ensure_fn_err_exit(self):
        raise AssertionError("existing error target was lost")
    def _slot_call_result_sink(self, expr):
        return self.sink
    def _new_slot_call_root(self, label):
        root = object()
        self.operations.append(("root", root))
        self.slots[root] = None
        return root
    def _slot_call_cleanup_block(self, roots, target):
        self.operations.append(("cleanup", roots, target))
        return (roots, target)
    def _emit_globals_builtin(self):
        self.operations.append(("retain-result", self.value))
        return self.value
    def _publish_slot_call_owned(self, output, value, label):
        assert self.operations[-1] == ("retain-result", value)
        self.operations.append(("publish", output, value))
        self.slots[output] = value
    def _emit_post_call_err_check(self, span):
        self.operations.append(("error-check", self._try_err_block))
        if self.pending:
            raise ValueError("original TLS error")
    def _load(self, output, name):
        self.operations.append(("load", output))
        return self.slots[output]
    def _fresh(self, label):
        return label
    def _guard_cpy_value_not_null(self, value):
        self.operations.append(("null-check", value))
        if value is None:
            raise MemoryError("namespace allocation failed")
    def _take_slot_call_root(self, output):
        self.operations.append(("take", output))
        return self.slots[output]


@pytest.mark.parametrize("with_sink", (False, True))
def test_globals_host_model_publishes_real_owner_first(with_sink):
    sink = object() if with_sink else None
    host = _Host(sink)
    result = _host_branch()[0](host, SimpleNamespace(args=(), kwargs=(), span=None))
    assert result is host.value
    events = [row[0] for row in host.operations]
    position = events.index("retain-result")
    assert events[position + 1] == "publish"
    assert events.index("error-check") > position + 1
    assert events.index("null-check") > events.index("error-check")
    assert ("take" in events) is (not with_sink)
    assert host._try_err_block == "outer-error"
    assert host._cpy_operand_cleanup_block == "outer-cpy"


@pytest.mark.parametrize("with_sink", (False, True))
@pytest.mark.parametrize("failure", ("pending", "null"))
def test_globals_host_model_keeps_error_root_and_restores_emitter(with_sink, failure):
    host = _Host(object() if with_sink else None, pending=failure == "pending", null=failure == "null")
    error = ValueError if failure == "pending" else MemoryError
    with pytest.raises(error):
        _host_branch()[0](host, SimpleNamespace(args=(), kwargs=(), span=None))
    events = [row[0] for row in host.operations]
    assert events[events.index("retain-result") + 1] == "publish"
    assert "take" not in events
    assert host._try_err_block == "outer-error"
    assert host._cpy_operand_cleanup_block == "outer-cpy"
    cleanup = next(row for row in host.operations if row[0] == "cleanup")
    assert len(cleanup[1]) == (0 if with_sink else 1)


@pytest.mark.parametrize("binding", ("local", "global", "function", "class", "alias"))
def test_globals_host_model_does_not_admit_a_shadow(binding):
    host = _Host()
    mapping = {"local": host.env, "global": host._module_globals,
               "function": host.functions, "class": host.class_lowering.classes,
               "alias": host._native_builtin_value_aliases}[binding]
    mapping["globals"] = object()
    assert _host_branch()[0](host, SimpleNamespace(args=(), kwargs=(), span=None)) is None
    assert host.operations == []


PROGRAM = '''import gc
marker = object()
__all__ = ['marker']
def namespace_keys():
    return sorted(list(globals().keys()) + __all__)
def discard_namespace():
    globals()
    gc.collect()
    return "marker" in globals()
def saved_view():
    namespace = globals()
    view = namespace.keys()
    del namespace
    gc.collect()
    return view
def take(first, second):
    return first
def fail_later():
    gc.collect()
    raise ValueError("later globals consumer")
def error_path():
    try:
        take(globals(), fail_later())
    except ValueError as error:
        assert str(error) == "later globals consumer"
    else:
        raise AssertionError("later error lost")
    gc.collect()
    return globals()
'''
SHADOWED = '''def globals():
    return {'custom': 1}
def named_shadow():
    return list(globals().keys())
def local_shadow(globals):
    return list(globals().keys())
'''


def test_globals_host_reference_views_and_shadowed_calls():
    namespace = {}
    exec(PROGRAM, namespace)
    assert namespace["discard_namespace"]() is True
    view = namespace["saved_view"]()
    gc.collect()
    assert "marker" in view
    assert namespace["error_path"]() is namespace
    assert namespace["namespace_keys"]() == sorted(list(namespace) + ["marker"])
    shadowed = {}
    exec(SHADOWED, shadowed)
    assert shadowed["named_shadow"]() == ["custom"]
    assert shadowed["local_shadow"](lambda: {"other": 2}) == ["other"]


def _function(text, name):
    match = re.search(r"(?ms)^define [^\n]*@user_[^\n(]*_" + re.escape(name)
                      + r"\([^\n]*\n.*?^\}", text)
    assert match is not None, name
    return match.group(0)


def _assert_discarded_globals_protocol(body):
    """Follow the two no-sink results, not unrelated cleanup in the function."""
    publications = re.findall(
        r"(?m)^  (%globals\.result\.retain[^ ]+) = call ptr[^\n]*@pcc_gc_retain\([^\n]*\)\n"
        r"  store ptr \1, ptr (%[^\n]+)$", body)
    assert len(publications) == 2, "both globals owners must be immediately rooted"
    blocks = dict(re.findall(r"(?ms)^([\w.]+):\n(.*?)(?=^[\w.]+:|^\})", body))
    aliases = dict(re.findall(r"(?m)^  (%[^ ]+) = bitcast ptr (%[^ ]+) to ptr$", body))

    def calls_on_slot(text, symbol, slot):
        return [match for match in re.finditer(
            r"@" + symbol + r"\(ptr (%[^,) ]+)(?:,|\))", text)
                if aliases.get(match.group(1), match.group(1)) == slot]

    for index, (_retained, slot) in enumerate(publications):
        cleanups = [(name, block) for name, block in blocks.items()
                    if calls_on_slot(block, "py_cleanup_one_root_preserving_exception", slot)]
        assert len(cleanups) == 1, "each result needs its own exception-preserving cleanup"
        cleanup_name, cleanup = cleanups[0]
        helper = calls_on_slot(cleanup, "py_cleanup_one_root_preserving_exception", slot)
        leaves = calls_on_slot(cleanup, "pcc_gc_frame_leave_lifo", slot)
        assert len(helper) == len(leaves) == 1 and helper[0].end() < leaves[0].start()

        # The singleton helper performs the TLS swap internally. Both the
        # producer-error and NULL edges must reach it with this exact root.
        null_guard = re.search(
            r"(?m)^  (%globals\.current[^ ]+) = load ptr, ptr " + re.escape(slot)
            + r"\n  (%cpy\.arg\.isnull[^ ]+) = icmp eq ptr \1, null\n"
            r"  br i1 \2, label %([^, ]+), label %([^\n ]+)", body)
        assert null_guard is not None and null_guard.group(3) == cleanup_name
        null_block = next(name for name, block in blocks.items() if null_guard.group() in block)
        error_check = re.search(
            r"(?m)^  (%err\.flag[^ ]+) = call i64 \(\) @py_err_occurred\(\)\n"
            r"  (%err\.cmp[^ ]+) = icmp ne i64 \1, 0\n"
            r"  br i1 \2, label %([^, ]+), label %" + re.escape(null_block) + r"$", body)
        assert error_check is not None, "producer TLS check must precede the NULL guard"
        error_target = error_check.group(3)
        assert (error_target == cleanup_name or
                blocks[error_target].rstrip().endswith("br label %" + cleanup_name))

        takes = calls_on_slot(body, "pcc_gc_take_pinned_slot", slot)
        assert len(takes) == 1, "success must transfer this root's owner exactly once"
        if index == 0:
            take_line = body.rfind("\n", 0, takes[0].start()) + 1
            take = re.match(r"  (%[^ ]+) = [^\n]+\n", body[take_line:])
            assert take is not None
            after_take = body[take_line + take.end():]
            release = re.match(r"  call void \(ptr\) @pcc_gc_release(?:_known)?\(ptr "
                               + re.escape(take.group(1)) + r"\)\n", after_take)
            assert release is not None, "discard must release the transferred owner before parking"
            assert after_take.index("@pcc_gc_collect(") >= release.end()


def _assert_discard_contract_rejects_mutations(body):
    # Sensitivity controls use the actual generated body. They only test the
    # assertions; these deliberately damaged strings are never compiled/run.
    mutations = (
        (r"(@pcc_gc_retain\([^\n]+\)\n)(  store ptr)",
         r"\1  call void () @pcc_gc_safepoint()\n\2"),
        (r"@py_cleanup_one_root_preserving_exception\(ptr %[^)]+\)",
         "@py_cleanup_one_root_preserving_exception(ptr null)"),
        (r"@pcc_gc_frame_leave_lifo\(", "@removed_frame_leave("),
        (r"(br i1 %err\.cmp[^,]+, label )%err\.frame[^,]+",
         r"\1%err.exit"),
        (r"(br i1 %cpy\.arg\.isnull[^,]+, label )%call\.slot\.cleanup[^,]+",
         r"\1%err.exit"),
        (r"@pcc_gc_release_known\(ptr %call\.slot\.take[^)]+\)",
         "@pcc_gc_release_known(ptr null)"),
        (r"(  call void \(ptr\) @pcc_gc_release_known\(ptr %call\.slot\.take[^\n]+)",
         r"  call void () @pcc_gc_safepoint()\n\1"),
    )
    for pattern, replacement in mutations:
        damaged, count = re.subn(pattern, replacement, body, count=1)
        assert count == 1, pattern
        with pytest.raises(AssertionError):
            _assert_discarded_globals_protocol(damaged)


@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu"))
def test_globals_owned_result_ir(tmp_path, monkeypatch, target):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    for label, source in (("globals_owner", PROGRAM), ("globals_shadow", SHADOWED)):
        path = tmp_path / (label + ".py")
        path.write_text(source)
        output = path.with_suffix(".ll")
        compile_python(str(path), str(output), emit_llvm_only=True, python_library=True,
                       libpython_mode="off", ir_scaffold_mode="on", backend="self", target_triple=target)
        text = output.read_text()
        verify_ir_text(text)
        if label == "globals_owner":
            for name in ("namespace_keys", "discard_namespace", "saved_view", "error_path"):
                body = _function(text, name)
                # The retained result is stored in its authoritative root in
                # the immediately following instruction, before any safepoint.
                assert re.search(r"(?m)^  (%[^ ]+) = call ptr[^\n]*@pcc_gc_retain\([^\n]*\)\n"
                                 r"  store ptr \1, ptr %[^\n]+", body), body
                assert "@pcc_gc_foreign_lease_acquire" in body
                assert "@pcc_gc_foreign_lease_release" in body
                assert "@pcc_gc_store_root" in body
                if name == "discard_namespace":
                    _assert_discarded_globals_protocol(body)
                    _assert_discard_contract_rejects_mutations(body)
                else:
                    assert "@py_tls_exc_swap_slot" in body
        else:
            for name in ("named_shadow", "local_shadow"):
                body = _function(text, name)
                assert not re.search(r"\bcall\b[^\n]*@py_module_attrs_dict\(", body), body
