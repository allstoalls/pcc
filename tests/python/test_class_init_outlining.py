"""Host structural checks for class-construction outlining.

These use the real owned parser, inference, code generator, optimizer and IR
verifier. They do not link or execute emitted code and cannot establish native
identity, collection safety, exception behavior or a performance improvement.
"""

from collections import Counter
import re

import pytest

from pcc.frontends.python.codegen.class_gen import ClassLowering
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.codegen.stmt_dispatch_lowering import StmtDispatchLoweringMixin
from pcc.frontends.python.compiled_owned_passes import run_owned_passes
from pcc.frontends.python.pipeline_pass_config import PYTHON_IR_PASS_DEFAULT_TIER
from pcc.frontends.python.py_ast import FuncDef
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from pcc.ir.compat import ir
from tests.owned_ir_validation import verify_ir_text


PREFIX = "__pcc_class_init_body_"
MODULE = "outline_probe"
INIT = "_pcc_py_module_init_" + MODULE
TOP = "_pcc_py_module_top_" + MODULE
TARGET = "x86_64-unknown-linux-gnu"
SIMPLE = '''class First(object):
    names = ('first', ('nested',))
    def take(self, value=None):
        return value
def between():
    return None
class Second(First):
    names = ('second',)
    def take(self, value=('default',)):
        return value
'''


@pytest.fixture(autouse=True)
def _bounded_host_configuration(monkeypatch):
    # Select the textual owned representation deliberately; no native worker,
    # runtime archive, external optimizer, target emitter or subprocess is used.
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    monkeypatch.delenv("PCC_DEBUG_CODEGEN_PHASES", raising=False)


def _generator(source, *, library=True):
    parsed = parse_and_lift(source, "outline_probe.py", MODULE)
    typed = infer_module(parsed)
    generator = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._strict_no_libpython = True
    generator._prefer_native_callable_values = True
    generator._skip_program_main = library
    generator._target_triple = TARGET
    generator.module.triple = TARGET
    generator._python_library = library
    return parsed, typed, generator


def _emit(source=SIMPLE, *, library=True):
    parsed, typed, generator = _generator(source, library=library)
    text = str(generator.generate(typed))
    verify_ir_text(text)
    assert "strict.nolib.stub:" not in text
    assert not re.search(r"\bcall[^\n]*@py_cpy_", text)
    return parsed, typed, generator, text


def _bodies(text):
    result = {}
    for match in re.finditer(r"(?ms)^define[^\n]*@([\w.$-]+)\([^\n]*\)[^\n]*\{\n.*?^}", text):
        assert match.group(1) not in result
        result[match.group(1)] = match.group(0)
    return result


def _calls(body):
    return re.findall(r"\bcall\b[^\n]*?@([\w.$-]+)\(", body)


def _helpers(text):
    return {name: body for name, body in _bodies(text).items() if name.startswith(PREFIX)}


def _outline_calls(body):
    return [name for name in _calls(body) if name.startswith(PREFIX)]


def _assert_status_boundary(body, helper):
    match = re.search(r"(%[-\w.]+) = call i32(?: \(\))? @" + re.escape(helper) + r"\(\)", body)
    assert match, (helper, body)
    tail = body[match.end():]
    branch = re.search(r"\bbr i1 (%[-\w.]+), label %([-\w.]+), label %([-\w.]+)", tail)
    assert branch, (helper, tail)
    boundary = tail[:branch.end()]
    comparison = re.search(
        r"(%[-\w.]+) = icmp eq i32 " + re.escape(match.group(1)) + r", 1\b",
        boundary,
    )
    assert comparison and branch.group(1) == comparison.group(1), (helper, boundary)
    assert branch.group(2) != branch.group(3)
    assert branch.group(3) == "err.exit", (helper, branch.groups())
    assert re.search(r"(?m)^" + re.escape(branch.group(3)) + r":", body)
    assert not re.search(r"\bcall\b", boundary), "outline call added a TLS poll/frame/activation before its status branch"


def _assert_definition_order_cfg(body, first, second):
    # Function.blocks is insertion ordered; a join may be printed before the
    # slow-path blocks that eventually branch back to it. Follow CFG edges.
    blocks = dict(re.findall(r"^([-\w.$]+):\n(.*?)(?=^[-\w.$]+:|^})", body, re.M | re.S))
    rows = {name: [line.strip() for line in text.splitlines() if line.strip()] for name, text in blocks.items()}
    assert "entry" in blocks
    successors = {name: re.findall(r"label %([-\w.$]+)", lines[-1]) for name, lines in rows.items()}
    assert all(target in blocks for targets in successors.values() for target in targets)

    def reachable(start, omitted=()):
        seen, pending = set(), [start]
        while pending:
            name = pending.pop()
            if name in seen or name in omitted:
                continue
            seen.add(name)
            pending.extend(successors[name])
        return seen

    def site(*tokens):
        found = [(name, index) for name, lines in rows.items() for index, line in enumerate(lines)
                 if all(token in line for token in tokens)]
        assert len(found) == 1, (tokens, found)
        assert found[0][0] in reachable("entry"), found
        return found[0]

    def precedes(before, after):
        owner, index = before
        use, use_index = after
        if owner == use:
            assert index < use_index, (before, after)
        else:
            assert use not in reachable("entry", (owner,)), (before, after)

    first_site = site("call i32", "@" + first + "(")
    binding = site("@py_module_attr_set(", "@.pyattr.between,")
    second_site = site("call i32", "@" + second + "(")
    adapter = "@user_outline_probe_between_native_adapter,"
    constructor_call = next(
        call for call in ("@py_func_new_signature_slots(", "@py_func_new_static_def(")
        if any(call in line and adapter in line for lines in rows.values() for line in lines)
    )
    constructor = site(constructor_call, adapter)
    precedes(first_site, binding)
    precedes(binding, second_site)
    precedes(first_site, constructor)
    assert binding[0] in reachable(constructor[0])
    assert constructor[0] not in reachable(binding[0])

    def conditional(block):
        branch = re.fullmatch(r"br i1 (%[-\w.$]+), label %([-\w.$]+), label %([-\w.$]+)", rows[block][-1])
        assert branch, (block, rows[block][-1])
        return branch.groups()

    cache = site(" = load ptr, ptr @__pcc_native_func_value_cache_outline_probe_between")
    cache_value = rows[cache[0]][cache[1]].split(" = ", 1)[0]
    condition, cached, created = conditional(cache[0])
    assert condition + " = icmp ne ptr " + cache_value + ", null" in rows[cache[0]]
    # Reuse legitimately bypasses construction, but both successful routes
    # must reach the same binding before the second class definition.
    assert binding[0] in reachable(cached, (constructor[0],))
    assert binding[0] in reachable(created)
    assert binding[0] not in reachable(created, (constructor[0],))
    status = rows[constructor[0]][constructor[1]].split(" = ", 1)[0]
    condition, failed, ready = conditional(constructor[0])
    assert condition + " = icmp slt i64 " + status + ", 0" in rows[constructor[0]]
    assert binding[0] in reachable(ready)
    assert binding[0] not in reachable(failed)
    assert second_site[0] not in reachable(failed)


def _reachable_integer_returns(body, entry):
    blocks = {}
    current = None
    for line in body.splitlines()[1:-1]:
        label = re.match(r"^([-\w.$]+):", line)
        if label:
            current = label.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    pending, visited, returns = [entry], set(), set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        assert name in blocks, (name, sorted(blocks))
        for line in blocks[name]:
            returns.update(re.findall(r"\bret i32 (-?\d+)\b", line))
            if re.search(r"\bbr\b", line):
                pending.extend(re.findall(r"label %([-\w.$]+)", line))
    return returns


@pytest.mark.parametrize("library", [True, False], ids=["module-top", "program-main"])
@pytest.mark.parametrize("extra_pass", [None, "inline", "inline-defined"], ids=["default-owned", "inline", "inline-defined"])
def test_one_construction_body_and_two_calls_survive_owned_passes(library, extra_pass):
    _, _, _, text = _emit(library=library)
    helpers = _helpers(text)
    assert len(helpers) == 2
    passes = list(PYTHON_IR_PASS_DEFAULT_TIER)
    if extra_pass is not None:
        passes.append(extra_pass)
    optimized = run_owned_passes(text, passes, strict_no_libpython=True)
    verify_ir_text(optimized)
    assert set(_helpers(optimized)) == set(helpers)
    for checked in (text, optimized):
        bodies = _bodies(checked)
        wrapper = TOP if library else "main"
        assert re.search(r"\bvoid @" + INIT + r"\(\)", bodies[INIT].splitlines()[0])
        if library:
            assert re.search(r"\bvoid @" + TOP + r"\(\)", bodies[TOP].splitlines()[0])
        else:
            assert re.search(r"\bi32 @main\(i32 [^,]+, ptr [^)]+\)", bodies["main"].splitlines()[0])
        assert _outline_calls(bodies[INIT]) == _outline_calls(bodies[wrapper])
        assert len(_outline_calls(bodies[INIT])) == 2
        for name, helper in _helpers(checked).items():
            header = helper.splitlines()[0]
            assert re.search(r"\binternal i32 @" + re.escape(name) + r"\(\)", header)
            assert "noinline" in header
            assert _calls(helper).count("py_class_new") == 1
            assert re.search(r"\bret i32 1\b", helper)
            assert re.search(r"\bret i32 0\b", helper)
            assert sum(_calls(body).count(name) for body in bodies.values()) == 2
            _assert_status_boundary(bodies[INIT], name)
            _assert_status_boundary(bodies[wrapper], name)


@pytest.mark.parametrize("other_attribute", [None, "alwaysinline"], ids=["empty", "other-only"])
def test_reused_helper_without_noinline_is_rejected(monkeypatch, other_attribute):
    original = ClassLowering._maybe_emit_class_init_outline
    changed = []

    def reject_reuse(self, cd, info):
        parent = self.parent
        if parent.current_function.name == TOP:
            helpers = [value for name, value in parent.module.globals.items() if name.startswith(PREFIX)]
            assert len(helpers) == 1
            helper = helpers[0]
            assert isinstance(helper, ir.Function)
            assert helper.module is parent.module
            assert str(helper.function_type) == str(ir.FunctionType(ir.IntType(32), [], var_arg=False))
            assert helper.linkage == "internal" and helper.blocks
            assert "noinline" in helper.attributes._attrs
            # Use the actual owned attributes object, not an iterable stand-in.
            # Empty and unrelated nonempty attributes must both fail closed.
            helper.attributes = ir.FunctionAttributes()
            if other_attribute is not None:
                helper.attributes.add(other_attribute)
            assert helper.attributes._attrs == ([] if other_attribute is None else [other_attribute])
            changed.append(helper.name)
        return original(self, cd, info)

    monkeypatch.setattr(ClassLowering, "_maybe_emit_class_init_outline", reject_reuse)
    with pytest.raises(L1CodegenError) as raised:
        _emit("class Admitted:\n    value = ('closed',)\n")
    assert len(changed) == 1
    assert str(raised.value) == "incompatible class-init outline declaration: " + changed[0]


@pytest.mark.parametrize("threads", ["0", "1"])
def test_inline_oracle_preserves_constructors_and_root_operations(monkeypatch, threads):
    monkeypatch.setenv("PCC_WITH_THREADS", threads)
    _, _, outlined_generator, outlined = _emit()
    assert outlined_generator._runtime_threads_enabled == (threads == "1")
    with monkeypatch.context() as disabled:
        disabled.setattr(ClassLowering, "_maybe_emit_class_init_outline", lambda self, cd, info: False)
        _, _, inline_generator, inline = _emit()
        assert inline_generator._runtime_threads_enabled == (threads == "1")
    assert not _helpers(inline)
    constructors = Counter()
    selected = {
        "py_class_new", "py_func_new_signature_slots", "py_func_new_static_def",
        "py_tuple_new", "py_class_setattr_raw", "py_class_add_method",
        "py_class_abort_definition_slots",
    }
    for helper in _helpers(outlined).values():
        calls = _calls(helper)
        constructors.update(name for name in calls if name in selected)
        assert "pcc_gc_frame_enter_lifo" in calls
        assert "pcc_gc_frame_leave_lifo" in calls
        assert "pcc_gc_foreign_lease_acquire" in calls
        assert "pcc_gc_foreign_lease_release" in calls
        assert "py_recursion_enter" not in calls
        assert "py_recursion_leave" not in calls
        assert "py_func_new_signature_slots" in calls or "py_func_new_static_def" in calls
    # The class-only oracle contains no interleaved module function binding.
    oracle = Counter(name for name in _calls(_bodies(inline)[INIT]) if name in selected)
    assert constructors == oracle
    assert Counter(_calls(outlined))["py_recursion_enter"] == Counter(_calls(inline))["py_recursion_enter"]
    assert Counter(_calls(outlined))["py_recursion_leave"] == Counter(_calls(inline))["py_recursion_leave"]
    for name in (INIT, TOP):
        assert "py_class_new" not in _calls(_bodies(outlined)[name])
    for helper in _helpers(outlined).values():
        assert re.search(r"store ptr [^\n]*, ptr @\.class\.outline_probe\.(First|Second)\b", helper)


def test_imports_assignments_and_function_bindings_keep_source_order(monkeypatch):
    source = "import sys\nmarker = 'before'\n" + SIMPLE.replace("class Second", "marker = 'after'\nclass Second")
    seen = []
    original = StmtDispatchLoweringMixin._emit_stmt_impl

    def observe(self, stmt):
        if self.current_function.name == TOP:
            seen.append((type(stmt).__name__, getattr(stmt, "name", ""), stmt.span.line))
        return original(self, stmt)

    monkeypatch.setattr(StmtDispatchLoweringMixin, "_emit_stmt_impl", observe)
    parsed, _, _, text = _emit(source)
    expected = [(type(stmt).__name__, getattr(stmt, "name", ""), stmt.span.line) for stmt in parsed.body]
    assert seen == expected
    top = _bodies(text)[TOP]
    # Bind helper identities to original class positions, not their text order.
    first, second = [PREFIX + MODULE + "_" + str(index) for index, stmt in enumerate(parsed.body)
                     if type(stmt).__name__ == "ClassDef"]
    assert Counter(_outline_calls(top)) == Counter((first, second))
    _assert_definition_order_cfg(top, first, second)
    assert ".pcc.module.init.outline_probe" in top
    assert ".pcc.module.init.outline_probe" not in _bodies(text)[INIT]


def test_inferred_argument_types_do_not_reject_unannotated_methods():
    parsed, typed, _, text = _emit("class Admitted:\n    def take(self, value=None):\n        return value\n")
    original = next(stmt for stmt in parsed.body[0].body if isinstance(stmt, FuncDef))
    inferred = next(stmt for stmt in typed.body[0].body if isinstance(stmt, FuncDef))
    assert all(arg.annotation is None for arg in original.args)
    assert all(arg.annotation is not None for arg in inferred.args)
    assert len(_helpers(text)) == 1


EXCLUDED = [
    ("external-default", "outside = ()\nclass Rejected:\n    def take(self, value=outside):\n        return value\n"),
    ("class-namespace", "class Rejected:\n    value = ()\n    def take(self, item=value):\n        return item\n"),
    ("numeric-literal", "class Rejected:\n    value = 1\n"),
    ("mutable-default", "class Rejected:\n    def take(self, value=[]):\n        return value\n"),
    ("method-decorator", "class Rejected:\n    @staticmethod\n    def take(value=None):\n        return value\n"),
    ("class-decorator", "def decorate(cls):\n    return cls\n@decorate\nclass Rejected:\n    pass\n"),
    ("no-op-decorator", "from typing import final\n@final\nclass Rejected:\n    value = ('kept',)\n"),
    ("dataclass", "from dataclasses import dataclass\n@dataclass\nclass Rejected:\n    value: str = 'text'\n"),
    ("metaclass", "class Meta(type):\n    pass\nclass Rejected(metaclass=Meta):\n    value = ('kept',)\n"),
    ("inherited-metaclass", "class Meta(type):\n    pass\nclass Base(metaclass=Meta):\n    pass\nclass Rejected(Base):\n    pass\n"),
    ("module-block", "if True:\n    class Rejected:\n        value = ('kept',)\n"),
    ("empty-capture-local", "def factory():\n    class Rejected:\n        value = ('kept',)\n    return Rejected\n"),
]


@pytest.mark.parametrize("case,source", EXCLUDED, ids=[case for case, _ in EXCLUDED])
def test_explicit_excluded_shapes_keep_actual_inline_construction(monkeypatch, case, source):
    emissions = []
    original = ClassLowering._emit_class_init

    def observe(self, cd, info, **kwargs):
        emissions.append((cd.name, self.parent.current_function.name))
        return original(self, cd, info, **kwargs)

    monkeypatch.setattr(ClassLowering, "_emit_class_init", observe)
    _, _, generator, text = _emit(source + "\nclass Admitted:\n    value = ('closed',)\n")
    rejected = [(name, owner) for name, owner in emissions if name.startswith("Rejected")]
    assert rejected, case
    assert all(not owner.startswith(PREFIX) for _, owner in rejected), rejected
    admitted = [owner for name, owner in emissions if name == "Admitted"]
    assert len(admitted) == 1 and admitted[0].startswith(PREFIX)
    if case == "empty-capture-local":
        local = next(name for name in generator._hoisted_class_capture_params if name.startswith("Rejected"))
        assert generator._hoisted_class_capture_params[local] == ()
        assert all(owner != INIT for _, owner in rejected)
    assert _helpers(text)


@pytest.mark.parametrize("fail", [False, True], ids=["success", "generation-error"])
def test_helper_generation_isolates_and_restores_enclosing_state(monkeypatch, fail):
    original_outline = ClassLowering._maybe_emit_class_init_outline
    original_body = ClassLowering._emit_class_init
    observed = []
    dumped = []
    fields = (
        "builder", "current_function", "current_func_def", "_current_entry_block",
        "env", "env_class_hint", "env_class_object_hint", "env_list_elem_class_hint",
        "_cpy_env_flags", "_cpy_values", "_owned_cpy_values", "_exact_int_env_flags",
        "_owned_local_names", "_owned_local_has_value", "_owned_local_flag_slots",
        "_gc_rooted_local_names", "_gc_rooted_local_order", "_current_param_names",
        "_container_temp_root_slot_names", "loop_stack", "_active_handler_excs",
        "_try_err_block", "_cpy_operand_cleanup_block",
    )

    class StopOutline(Exception):
        pass

    def scoped(self, cd, info):
        parent = self.parent
        saved = {name: getattr(parent, name) for name in fields}
        flags = {"outline_outer": True}
        foreign = {object()}
        owned = {object()}
        parent._cpy_env_flags, parent._cpy_values, parent._owned_cpy_values = flags, foreign, owned
        expected = {name: getattr(parent, name) for name in fields}
        try:
            return original_outline(self, cd, info)
        finally:
            for name, value in expected.items():
                assert getattr(parent, name) is value, name
            assert flags == {"outline_outer": True}
            for name in ("_cpy_env_flags", "_cpy_values", "_owned_cpy_values"):
                setattr(parent, name, saved[name])

    def body(self, cd, info, **kwargs):
        parent = self.parent
        if parent.current_function.name.startswith(PREFIX):
            observed.append(parent.current_function.name)
            assert parent.current_func_def is None
            assert not parent.env
            assert not parent._cpy_env_flags
            assert not parent._cpy_values
            assert not parent._owned_cpy_values
            assert not parent._active_handler_excs
            assert getattr(parent, "_class_namespace_context", None) is None
            if fail:
                raise StopOutline()
        return original_body(self, cd, info, **kwargs)

    def observe_dump(self, exc):
        if isinstance(exc, StopOutline):
            function = self.current_function
            dumped.append(("" if function is None else function.name, self.current_func_def, self.env))

    monkeypatch.setattr(L1CodeGen, "_codegen_trace_dump", observe_dump)
    monkeypatch.setattr(ClassLowering, "_maybe_emit_class_init_outline", scoped)
    monkeypatch.setattr(ClassLowering, "_emit_class_init", body)
    source = "class Admitted:\n    value = ('closed',)\n"
    if fail:
        with pytest.raises(StopOutline):
            _emit(source)
    else:
        _emit(source)
    assert len(observed) == 1
    if fail:
        assert dumped and dumped[0][0].startswith(PREFIX)
        assert dumped[0][1] is None and not dumped[0][2]
    else:
        assert not dumped


@pytest.mark.parametrize("kind", ["native-exception", "bridge-null", "bridge-negative"])
def test_actual_helper_error_epilogue_returns_status_for_all_error_sources(monkeypatch, kind):
    original = ClassLowering._emit_class_init
    injected = []

    def emit(self, cd, info, **kwargs):
        parent = self.parent
        if parent.current_function.name.startswith(PREFIX):
            injected.append((parent.current_function.name, parent._ensure_fn_err_exit().name))
            if kind == "native-exception":
                parent._emit_builtin_exception_and_branch(
                    "ValueError", "structural outline error", cd.span,
                    open_dead_continuation=True,
                )
            elif kind == "bridge-null":
                parent._guard_cpy_value_not_null(ir.Constant(ir.IntType(8).as_pointer(), None))
            else:
                parent._guard_cpy_status_not_negative(ir.Constant(ir.IntType(32), -1))
        return original(self, cd, info, **kwargs)

    monkeypatch.setattr(ClassLowering, "_emit_class_init", emit)
    _, _, _, text = _emit("class Admitted:\n    value = ('closed',)\n")
    assert len(injected) == 1
    name, error_entry = injected[0]
    helper = _helpers(text)[name]
    assert re.search(r"\bret i32 0\b", helper)
    assert re.search(r"\bret i32 1\b", helper)
    assert _reachable_integer_returns(helper, error_entry) == {"0"}
    assert re.search(r"\b(?:br label|label) %" + re.escape(error_entry) + r"\b", helper)
    for wrapper in (INIT, TOP):
        _assert_status_boundary(_bodies(text)[wrapper], name)
    # These are injected code-generation branches, not native runtime faults.
    if kind == "native-exception":
        assert "py_raise" in _calls(helper)
    elif kind == "bridge-null":
        assert re.search(r"icmp eq ptr null, null", helper)
    else:
        assert re.search(r"icmp slt i32 -1, 0", helper)
