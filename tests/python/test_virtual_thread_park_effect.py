"""Closed-world virtual-thread ``may_park`` propagation and delegation."""

from __future__ import annotations

import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from pcc1_gate import find_current_pcc1


REPO = Path(__file__).resolve().parents[2]


TRANSITIVE_SOURCE = '''import pcc.virtual_thread as vt

def leaf(value: int) -> int:
    live_across_park: int = value + 1
    vt.sleep_current(0)
    return live_across_park + 1

def middle(value: int) -> int:
    parent_live_across_child: int = 3
    try:
        child_result: int = leaf(value)
        return child_result + parent_live_across_child
    finally:
        print("FINALLY")

def handler() -> int:
    return middle(20) * 2

def main() -> None:
    thread = vt.spawn(handler)
    vt.run(1, 32)
    print(vt.state(thread))
    print(vt.result(thread))

if __name__ == "__main__":
    main()
'''


EXCEPTION_SOURCE = '''import pcc.virtual_thread as vt

def failing_leaf() -> int:
    vt.sleep_current(0)
    raise ValueError("parked boom")

def catches_transitive_failure() -> int:
    try:
        return failing_leaf()
    except ValueError:
        return 41
    finally:
        print("CAUGHT_FINALLY")

def handler() -> int:
    return catches_transitive_failure() + 1

def main() -> None:
    thread = vt.spawn(handler)
    vt.run(1, 32)
    print(vt.result(thread))

if __name__ == "__main__":
    main()
'''


CROSS_MODULE_LEAF_SOURCE = '''import pcc.virtual_thread as vt

def parked_leaf(value: int) -> int:
    saved: int = value + 1
    vt.yield_now()
    return saved + 1
'''


CROSS_MODULE_MAIN_SOURCE = '''import pcc.virtual_thread as vt
from park_effect_leaf import parked_leaf

def handler() -> int:
    return parked_leaf(40)

def main() -> None:
    thread = vt.spawn(handler)
    vt.run(1, 32)
    print(vt.result(thread))

if __name__ == "__main__":
    main()
'''


METHOD_SOURCE = '''import pcc.virtual_thread as vt

class Worker:
    def leaf(self, value: int) -> int:
        saved: int = value + 1
        vt.yield_now()
        return saved

    def middle(self, value: int) -> int:
        return self.leaf(value) + 1

def handler() -> int:
    return Worker().middle(40)

def main() -> None:
    thread = vt.spawn(handler)
    vt.run(1, 32)
    print(vt.result(thread))

if __name__ == "__main__":
    main()
'''


CROSS_MODULE_METHOD_LEAF_SOURCE = '''import pcc.virtual_thread as vt

class Worker:
    def run(self, value: int) -> int:
        vt.yield_now()
        return value + 1
'''


CROSS_MODULE_METHOD_MAIN_SOURCE = '''import pcc.virtual_thread as vt
from park_method_leaf import Worker

def handler() -> int:
    return Worker().run(41)

def main() -> None:
    thread = vt.spawn(handler)
    vt.run(1, 32)
    print(vt.result(thread))

if __name__ == "__main__":
    main()
'''


DYNAMIC_CALLBACK_SOURCE = '''import pcc.virtual_thread as vt

def ordinary_generator(value: int):
    yield value

def parked_callback(value: int) -> int:
    saved: int = value + 1
    vt.yield_now()
    return saved + 1

def dispatch_callback(callback, value: int):
    return vt.call(callback, value)

def handler() -> int:
    plain = dispatch_callback(ordinary_generator, 7)
    plain_value: int = next(plain)
    parked_value: int = dispatch_callback(parked_callback, 40)
    return plain_value + parked_value

def main() -> None:
    thread = vt.spawn(handler)
    vt.run(1, 64)
    print(vt.result(thread))

if __name__ == "__main__":
    main()
'''


def _compile_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_text: str,
    name: str,
    *,
    emit_llvm_only: bool = False,
) -> Path:
    monkeypatch.setenv("PCC_RUNTIME_CC", "cc")
    monkeypatch.setenv("PCC_RUNTIME_HIGH", "c")
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / f"{name}.py"
    output = tmp_path / (f"{name}.ll" if emit_llvm_only else name)
    source.write_text(source_text, encoding="utf-8")
    compile_python(
        str(source),
        str(output),
        emit_llvm_only=emit_llvm_only,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    return output


def test_closed_world_analysis_propagates_may_park_to_all_direct_callers() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.native_modules import _is_virtual_thread_export
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        classify_vthread_park_boundaries,
        compute_vthread_may_park_functions,
    )

    module = parse(
        '''import pcc.virtual_thread as vt
from pcc.virtual_thread import readable as wait_readable

def leaf() -> int:
    vt.block_current_on_fd(3, 1, 10)
    return 1

def middle() -> int:
    return leaf() + 1

def handler() -> int:
    return middle() + 1

def unrelated() -> int:
    reference_only = leaf
    return 9

def shadowed_function(leaf) -> int:
    return leaf()

def shadowed_value(wait_readable) -> int:
    return wait_readable(3)

def rebound_value() -> int:
    wait_readable = leaf
    return wait_readable()

def shadowed_module(vt) -> int:
    return vt.readable(3)

def rebound_module() -> int:
    vt = leaf
    return vt.readable(3)

def local_value_import() -> None:
    from pcc.virtual_thread import writable as local_writable
    local_writable(4)

def local_module_import() -> None:
    import pcc.virtual_thread as local_vt
    local_vt.readable(4)

def local_import_caller() -> None:
    local_value_import()
    local_module_import()
''',
        "park_effect_analysis.py",
    )

    _func_ids, names = compute_vthread_may_park_functions(module)
    assert names == {
        "leaf",
        "middle",
        "handler",
        "local_value_import",
        "local_module_import",
        "local_import_caller",
    }
    rejected = classify_vthread_park_boundaries(module, names)
    assert set(rejected) == {
        "shadowed_function",
        "shadowed_value",
        "rebound_value",
        "shadowed_module",
        "rebound_module",
    }
    assert _is_virtual_thread_export("call") is True


def test_dynamic_callback_adapter_is_an_explicit_may_park_effect_root() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        compute_vthread_may_park_functions,
    )

    module = parse(DYNAMIC_CALLBACK_SOURCE, "park_effect_dynamic_callback.py")
    _function_ids, names = compute_vthread_may_park_functions(module)

    assert names == {"parked_callback", "dispatch_callback", "handler"}
    assert "ordinary_generator" not in names


def test_dynamic_callback_from_import_is_a_native_effect_alias() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        compute_vthread_may_park_functions,
    )

    module = parse(
        '''from pcc.virtual_thread import call

def dispatch(callback, value):
    return call(callback, value)
''',
        "park_effect_dynamic_callback_from_import.py",
    )
    _function_ids, names = compute_vthread_may_park_functions(module)
    assert names == {"dispatch"}


def test_nonparking_from_import_aliases_resolve_without_becoming_effect_roots() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        compute_vthread_may_park_functions,
        vthread_proven_value_alias,
    )

    module = parse(
        '''from pcc.virtual_thread import spawn as start, state

def dispatch(callback, thread):
    from pcc.virtual_thread import result as collect
    start(callback)
    collect(thread)
    return state(thread)
''',
        "park_effect_nonparking_from_import.py",
    )
    function = next(node for node in module.body if getattr(node, "name", "") == "dispatch")
    _function_ids, names = compute_vthread_may_park_functions(module)

    assert names == set()
    assert vthread_proven_value_alias(module, function, "start", "spawn")
    assert vthread_proven_value_alias(module, function, "collect", "result")
    assert vthread_proven_value_alias(module, function, "state", "state")


def test_dynamic_callback_ir_uses_distinct_may_park_generator_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llvm = _compile_host(
        tmp_path,
        monkeypatch,
        DYNAMIC_CALLBACK_SOURCE,
        "park_effect_dynamic_callback_ir",
        emit_llvm_only=True,
    )
    ir_text = llvm.read_text(encoding="utf-8")

    assert "py_gen_set_may_park" in ir_text
    assert "py_gen_is_may_park" in ir_text
    assert "__pcc_vthread_delegate_pcc_virtual_thread_call" in ir_text
    call_result = ir_text.index("vthread.call.result")
    result_root = ir_text.index("@pcc_gc_store_root", call_result)
    args_release = ir_text.index("@pcc_gc_release", result_root)
    marker_check = ir_text.index("@py_gen_is_may_park", args_release)
    inspect_reload = ir_text.rindex(
        "@pcc_gc_load_ptr", args_release, marker_check
    )
    direct_reload = ir_text.index("@pcc_gc_load_ptr", marker_check)
    direct_retain = ir_text.index("@pcc_gc_retain", direct_reload)
    assert (
        call_result
        < result_root
        < args_release
        < inspect_reload
        < marker_check
        < direct_reload
        < direct_retain
    )


def test_generator_close_reloads_gc4_roots_across_cleanup_safepoints() -> None:
    py_source = (REPO / "pcc" / "py_runtime" / "py" / "py_gen.py").read_text(
        encoding="utf-8"
    )
    c_source = (REPO / "pcc" / "py_runtime" / "src" / "py_gen.c").read_text(
        encoding="utf-8"
    )

    assert "gen = load_ptr(gen_slot, 0)" in py_source
    assert "closed = py_gen_close(load_ptr(gen_slot, 0))" in py_source
    assert "PyObject *closed = py_gen_close(rooted_gen);" in c_source
    assert "pcc_gc_store_root(&rooted_gen, gen);" in c_source


def test_dynamic_callback_delegates_parking_but_preserves_plain_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _compile_host(
        tmp_path,
        monkeypatch,
        DYNAMIC_CALLBACK_SOURCE,
        "park_effect_dynamic_callback_values",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "49"


def test_closed_world_metadata_propagates_across_compiled_sibling_imports() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        annotate_closed_world_vthread_effects,
        compute_vthread_may_park_functions,
    )

    leaf = parse(CROSS_MODULE_LEAF_SOURCE, "park_effect_leaf.py")
    caller = parse(CROSS_MODULE_MAIN_SOURCE, "park_effect_main.py")
    exports = {
        "park_effect_leaf": {
            "parked_leaf": {
                "kind": "function",
                "owning_module": "park_effect_leaf",
                "export_name": "parked_leaf",
            },
        },
        "park_effect_main": {
            "handler": {
                "kind": "function",
                "owning_module": "park_effect_main",
                "export_name": "handler",
            },
            "main": {
                "kind": "function",
                "owning_module": "park_effect_main",
                "export_name": "main",
            },
        },
    }
    annotate_closed_world_vthread_effects(
        [leaf, caller],
        ["park_effect_leaf", "park_effect_main"],
        exports,
    )

    assert exports["park_effect_leaf"]["parked_leaf"]["may_park"] is True
    assert exports["park_effect_main"]["handler"]["may_park"] is True
    _ids, names = compute_vthread_may_park_functions(caller, exports)
    assert "parked_leaf" in names
    assert "handler" in names


def test_summary_metadata_matches_eager_across_compiled_siblings(
    tmp_path: Path,
) -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        annotate_closed_world_vthread_effect_summaries,
        annotate_closed_world_vthread_effects,
        build_closed_world_vthread_effect_summary,
        closed_world_vthread_effect_export_surface,
        write_closed_world_vthread_effect_summary,
    )

    modules = [
        parse(CROSS_MODULE_LEAF_SOURCE, "park_effect_leaf.py"),
        parse(CROSS_MODULE_MAIN_SOURCE, "park_effect_main.py"),
    ]
    module_names = ["park_effect_leaf", "park_effect_main"]
    exports = {
        "park_effect_leaf": {
            "parked_leaf": {
                "kind": "function",
                "owning_module": "park_effect_leaf",
                "export_name": "parked_leaf",
            },
        },
        "park_effect_main": {
            name: {
                "kind": "function",
                "owning_module": "park_effect_main",
                "export_name": name,
            }
            for name in ("handler", "main")
        },
    }
    eager = deepcopy(exports)
    summarized = deepcopy(exports)
    annotate_closed_world_vthread_effects(modules, module_names, eager)

    surface = closed_world_vthread_effect_export_surface(summarized)
    paths = []
    for index, (module, module_name) in enumerate(zip(modules, module_names)):
        summary = build_closed_world_vthread_effect_summary(
            module,
            module_name,
            surface,
        )
        path = tmp_path / f"summary_{index}.wire"
        write_closed_world_vthread_effect_summary(str(path), summary)
        paths.append(str(path))

    count, nodes, edges = annotate_closed_world_vthread_effect_summaries(
        paths,
        summarized,
    )

    assert summarized == eager
    assert count == 2
    assert nodes >= 3
    assert edges >= 1


def test_summary_duplicate_definition_uses_last_binding(tmp_path: Path) -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        annotate_closed_world_vthread_effect_summaries,
        annotate_closed_world_vthread_effects,
        build_closed_world_vthread_effect_summary,
        closed_world_vthread_effect_export_surface,
        write_closed_world_vthread_effect_summary,
    )

    module = parse(
        '''import pcc.virtual_thread as vt

def leaf() -> int:
    vt.yield_now()
    return 1

def caller() -> int:
    return leaf()

def caller() -> int:
    return 2
''',
        "duplicate.py",
    )
    exports = {
        "duplicate": {
            name: {
                "kind": "function",
                "owning_module": "duplicate",
                "export_name": name,
            }
            for name in ("leaf", "caller")
        }
    }
    eager = deepcopy(exports)
    summarized = deepcopy(exports)
    annotate_closed_world_vthread_effects([module], ["duplicate"], eager)
    summary = build_closed_world_vthread_effect_summary(
        module,
        "duplicate",
        closed_world_vthread_effect_export_surface(summarized),
    )
    path = tmp_path / "duplicate.wire"
    write_closed_world_vthread_effect_summary(str(path), summary)
    annotate_closed_world_vthread_effect_summaries([str(path)], summarized)

    assert summarized == eager
    assert summarized["duplicate"]["leaf"]["may_park"] is True
    assert summarized["duplicate"]["caller"]["may_park"] is False


def test_closed_world_metadata_publishes_compiled_sibling_method_effect() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        annotate_closed_world_vthread_effects,
    )

    leaf = parse(CROSS_MODULE_METHOD_LEAF_SOURCE, "park_method_leaf.py")
    caller = parse(CROSS_MODULE_METHOD_MAIN_SOURCE, "park_method_main.py")
    method_row = {
        "name": "run",
        "kind": "instance",
        "return_ty": "int",
        "param_types": ("dyn", "int"),
    }
    exports = {
        "park_method_leaf": {
            "Worker": {
                "kind": "class",
                "methods": (method_row,),
            },
        },
        "park_method_main": {
            "handler": {
                "kind": "function",
                "owning_module": "park_method_main",
                "export_name": "handler",
            },
            "main": {
                "kind": "function",
                "owning_module": "park_method_main",
                "export_name": "main",
            },
        },
    }
    annotate_closed_world_vthread_effects(
        [leaf, caller],
        ["park_method_leaf", "park_method_main"],
        exports,
    )

    assert method_row["may_park"] is True


def test_eager_closed_world_effect_is_deterministic_across_fresh_asts() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        annotate_closed_world_vthread_effects,
    )

    def analyze():
        method_row = {
            "name": "run",
            "kind": "instance",
            "return_ty": "int",
            "param_types": ("dyn", "int"),
        }
        exports = {
            "park_method_leaf": {
                "Worker": {"kind": "class", "methods": (method_row,)},
            },
            "park_method_main": {
                "handler": {
                    "kind": "function",
                    "owning_module": "park_method_main",
                    "export_name": "handler",
                },
                "main": {
                    "kind": "function",
                    "owning_module": "park_method_main",
                    "export_name": "main",
                },
            },
        }
        annotate_closed_world_vthread_effects(
            [
                parse(CROSS_MODULE_METHOD_LEAF_SOURCE, "park_method_leaf.py"),
                parse(CROSS_MODULE_METHOD_MAIN_SOURCE, "park_method_main.py"),
            ],
            ["park_method_leaf", "park_method_main"],
            exports,
        )
        return exports

    assert analyze() == analyze()


def test_closed_world_effects_follow_package_reexported_function_and_class(
    tmp_path: Path,
) -> None:
    from pcc.py_frontend.pipeline_context import build_closed_world_context

    package = tmp_path / "park_api"
    package.mkdir()
    entry = tmp_path / "entry.py"
    facade = package / "__init__.py"
    leaf = package / "leaf.py"
    entry.write_text(
        "from park_api import Worker, parked\n"
        "def through_function() -> int:\n"
        "    return parked(40)\n"
        "def through_method() -> int:\n"
        "    return Worker().run(41)\n",
        encoding="utf-8",
    )
    facade.write_text(
        "from .leaf import Worker, parked\n",
        encoding="utf-8",
    )
    leaf.write_text(
        "import pcc.virtual_thread as vt\n"
        "def parked(value: int) -> int:\n"
        "    vt.yield_now()\n"
        "    return value + 2\n"
        "class Worker:\n"
        "    def run(self, value: int) -> int:\n"
        "        vt.yield_now()\n"
        "        return value + 1\n",
        encoding="utf-8",
    )

    _modules, exports, _derived = build_closed_world_context(
        [str(entry), str(facade), str(leaf)],
        ["entry", "park_api", "park_api.leaf"],
    )

    assert exports["entry"]["through_function"]["may_park"] is True
    assert exports["entry"]["through_method"]["may_park"] is True


def test_user_method_parking_boundary_is_rejected_not_guessed_resumable() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        classify_vthread_park_boundaries,
        compute_vthread_may_park_functions,
    )

    module = parse(
        '''import pcc.virtual_thread as vt

def leaf() -> None:
    vt.yield_now()

class Worker:
    def park(self) -> None:
        leaf()

def dynamic_entry(worker) -> None:
    worker.park()
''',
        "park_effect_method_boundary.py",
    )
    _ids, may_park = compute_vthread_may_park_functions(module)
    rejected = classify_vthread_park_boundaries(module, may_park)

    assert may_park == {"leaf"}
    assert rejected == {
        "dynamic_entry": "unresolved user-method may park: .park (receiver type 'dyn', module None)",
    }


def test_concrete_local_method_chain_joins_closed_world_may_park_fixed_point() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        classify_vthread_park_boundaries,
        compute_vthread_may_park_functions,
        compute_vthread_may_park_methods,
    )

    module = parse(METHOD_SOURCE, "park_effect_method.py")
    _func_ids, functions = compute_vthread_may_park_functions(module)
    _method_ids, methods = compute_vthread_may_park_methods(module, functions)
    rejected = classify_vthread_park_boundaries(
        module,
        functions,
        None,
        methods,
    )

    assert functions == {"handler"}
    assert methods == {"Worker.leaf", "Worker.middle"}
    assert rejected == {}


def test_joint_callable_analysis_scans_threading_hints_once_per_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen import vthread_effect_analysis as analysis

    methods = [
        "    def setup(self) -> None:\n"
        "        self.event = threading.Event()\n",
        "    def wait(self) -> None:\n"
        "        self.event.wait()\n",
    ]
    for index in range(20):
        methods.append(
            "    def helper_"
            + str(index)
            + "(self, value: int) -> int:\n"
            + "        return value + "
            + str(index)
            + "\n"
        )
    module = parse(
        "import threading\n\nclass Worker:\n" + "\n".join(methods),
        "vthread_linear_analysis.py",
    )

    calls = 0
    original = analysis._threading_assignment_hints

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis, "_threading_assignment_hints", counted)
    _function_ids, _functions, _method_ids, method_keys = (
        analysis.compute_vthread_may_park_callables(module)
    )

    assert calls == 22
    assert "Worker.wait" in method_keys


def test_callable_compute_and_classify_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen import vthread_effect_analysis as analysis

    module = parse(
        '''import pcc.virtual_thread as vt
import threading

def leaf() -> None:
    vt.yield_now()

def wrapper() -> None:
    leaf()

def shadowed(vt) -> None:
    vt.yield_now()

class Worker:
    def setup(self) -> None:
        self.event = threading.Event()

    def wait(self) -> None:
        self.event.wait()

    def dynamic(self, other) -> None:
        other.wait()
''',
        "vthread_shared_kernel.py",
    )
    callable_count = len(analysis._module_function_defs(module)) + len(
        analysis._class_method_defs(module)
    )
    scans = 0
    original = analysis._function_scope_nodes

    def counted(*args, **kwargs):
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis, "_function_scope_nodes", counted)
    first_effects = analysis.compute_vthread_may_park_callables(module)
    first_rejected = analysis.classify_vthread_park_boundaries(
        module,
        first_effects[1],
        None,
        first_effects[3],
    )
    assert scans == callable_count * 2

    scans = 0
    second_effects = analysis.compute_vthread_may_park_callables(module)
    second_rejected = analysis.classify_vthread_park_boundaries(
        module,
        second_effects[1],
        None,
        second_effects[3],
    )
    assert scans == callable_count * 2
    assert first_effects == second_effects
    assert first_rejected == second_rejected


def test_suspension_call_proof_reuses_one_lexical_binding_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen import vthread_effect_analysis as analysis

    module = parse(
        '''import pcc.virtual_thread as vt

def worker() -> None:
    vt.yield_now()
    vt.sleep_current(0)
''',
        "vthread_binding_cache.py",
    )
    fd = analysis._module_function_defs(module)[0]
    calls = analysis._function_attr_calls(fd)
    scans = 0
    original = analysis._function_vthread_bindings

    def counted(*args, **kwargs):
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis, "_function_vthread_bindings", counted)
    cache = {}
    assert analysis.vthread_proven_suspension_call_key(
        module, fd, calls[0], cache
    ) == "pcc.virtual_thread.yield_now"
    assert analysis.vthread_proven_suspension_call_key(
        module, fd, calls[1], cache
    ) == "pcc.virtual_thread.sleep_current"
    assert scans == 1


def test_effect_scope_walk_does_not_descend_into_semantic_type_metadata() -> None:
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend.py_ast import Type
    from pcc.py_frontend.type_infer import infer_module
    from pcc.py_frontend.codegen import vthread_effect_analysis as analysis

    module = infer_module(
        parse_and_lift(
            "def worker(value: int) -> int:\n"
            "    return value + 1\n",
            "vthread_scope_metadata.py",
            "vthread_scope_metadata",
        )
    )
    fd = analysis._module_function_defs(module)[0]

    nodes = analysis._function_scope_nodes(fd)

    assert nodes
    assert not any(isinstance(node, Type) for node in nodes)


def test_dynamic_receiver_inside_may_park_method_is_rejected_fail_closed() -> None:
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend.type_infer import infer_module
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        classify_vthread_park_boundaries,
        compute_vthread_may_park_functions,
        compute_vthread_may_park_methods,
    )

    module = infer_module(
        parse_and_lift(
            '''import pcc.virtual_thread as vt

class Worker:
    def park(self) -> None:
        vt.yield_now()

    def forward(self, dynamic_worker) -> None:
        dynamic_worker.park()

def handler(worker: Worker, dynamic_worker) -> None:
    worker.forward(dynamic_worker)
''',
            "park_effect_dynamic_method.py",
            "park_effect_dynamic_method",
        )
    )
    _func_ids, functions = compute_vthread_may_park_functions(module)
    _method_ids, methods = compute_vthread_may_park_methods(module, functions)
    rejected = classify_vthread_park_boundaries(
        module,
        functions,
        None,
        methods,
    )

    assert methods == {"Worker.park"}
    assert rejected["Worker.forward"] == (
        "unresolved user-method may park: .park (receiver type 'dyn', module None)"
    )
    assert rejected["handler"] == (
        "calls unresolved may_park method wrapper: Worker.forward: "
        "unresolved user-method may park: .park (receiver type 'dyn', module None)"
    )


def test_implicit_dunder_may_park_dispatch_is_rejected_fail_closed() -> None:
    from pcc.py_frontend.parser import parse
    from pcc.py_frontend.codegen.vthread_effect_analysis import (
        classify_vthread_park_boundaries,
        compute_vthread_may_park_functions,
        compute_vthread_may_park_methods,
    )

    module = parse(
        '''import pcc.virtual_thread as vt

class UnsafeConstructor:
    def __init__(self) -> None:
        vt.yield_now()
''',
        "park_effect_implicit_method.py",
    )
    _func_ids, functions = compute_vthread_may_park_functions(module)
    _method_ids, methods = compute_vthread_may_park_methods(module, functions)
    rejected = classify_vthread_park_boundaries(
        module,
        functions,
        None,
        methods,
    )
    assert rejected["UnsafeConstructor.__init__"] == (
        "implicit descriptor/dunder may_park dispatch is unsupported"
    )


def test_concrete_may_park_methods_use_managed_child_continuations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llvm = _compile_host(
        tmp_path,
        monkeypatch,
        METHOD_SOURCE,
        "park_effect_method_ir",
        emit_llvm_only=True,
    )
    ir_text = llvm.read_text(encoding="utf-8")

    assert ir_text.count("__gen_resume") >= 3
    assert "__pcc_vthread_delegate_Worker_leaf" in ir_text
    assert "__pcc_vthread_delegate_Worker_middle" in ir_text
    assert "py_gen_next" in ir_text


def test_concrete_may_park_method_chain_resumes_to_python_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _compile_host(
        tmp_path,
        monkeypatch,
        METHOD_SOURCE,
        "park_effect_method_values",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "42"


def test_transitive_may_park_lowers_to_managed_child_continuations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llvm = _compile_host(
        tmp_path,
        monkeypatch,
        TRANSITIVE_SOURCE,
        "park_effect_ir",
        emit_llvm_only=True,
    )
    ir_text = llvm.read_text(encoding="utf-8")

    # leaf, middle and handler all use heap-owned generator frames even though
    # none uses Python generator syntax.  Parent calls drive the child through
    # py_gen_next and recover its Python-visible return value.
    assert ir_text.count("__gen_resume") >= 3
    assert "__pcc_vthread_delegate_leaf" in ir_text
    assert "__pcc_vthread_delegate_middle" in ir_text
    assert "py_gen_next" in ir_text
    assert "py_exc_get_message" in ir_text
    assert "py_virtual_thread_resume_generator" in ir_text


def test_transitive_park_resume_preserves_live_values_and_finally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _compile_host(
        tmp_path,
        monkeypatch,
        TRANSITIVE_SOURCE,
        "park_effect_values",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip().splitlines() == ["FINALLY", "4", "50"]


def test_transitive_park_resume_routes_exception_through_parent_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _compile_host(
        tmp_path,
        monkeypatch,
        EXCEPTION_SOURCE,
        "park_effect_exception",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip().splitlines() == ["CAUGHT_FINALLY", "42"]


def test_cross_module_may_park_uses_generator_abi_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PCC_RUNTIME_CC", "cc")
    monkeypatch.setenv("PCC_RUNTIME_HIGH", "c")
    from pcc.py_frontend.pipeline import compile_python_multi

    leaf = tmp_path / "park_effect_leaf.py"
    main = tmp_path / "park_effect_main.py"
    executable = tmp_path / "park_effect_multi"
    leaf.write_text(CROSS_MODULE_LEAF_SOURCE, encoding="utf-8")
    main.write_text(CROSS_MODULE_MAIN_SOURCE, encoding="utf-8")
    compile_python_multi(
        [str(leaf), str(main)],
        str(executable),
        module_names=["park_effect_leaf", "park_effect_main"],
        entry_module="park_effect_main",
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "42"


def test_cross_module_may_park_method_uses_generator_abi_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PCC_RUNTIME_CC", "cc")
    monkeypatch.setenv("PCC_RUNTIME_HIGH", "c")
    from pcc.py_frontend.pipeline import compile_python_multi

    leaf = tmp_path / "park_method_leaf.py"
    main = tmp_path / "park_method_main.py"
    executable = tmp_path / "park_method_multi"
    leaf.write_text(CROSS_MODULE_METHOD_LEAF_SOURCE, encoding="utf-8")
    main.write_text(CROSS_MODULE_METHOD_MAIN_SOURCE, encoding="utf-8")
    compile_python_multi(
        [str(leaf), str(main)],
        str(executable),
        module_names=["park_method_leaf", "park_method_main"],
        entry_module="park_method_main",
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "42"


def test_parallel_cross_shard_two_init_reexports_preserve_function_and_method_abi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent export pass must join effects after both facades converge."""
    monkeypatch.setenv("PCC_RUNTIME_CC", "cc")
    monkeypatch.setenv("PCC_RUNTIME_HIGH", "c")
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "4")
    # The effect fixed point owns AST transport; correctness cannot depend on
    # the historical performance opt-in being set by the caller.
    monkeypatch.delenv("PCC_PY_FRONTEND_AST_WIRE", raising=False)
    from pcc.py_frontend.pipeline import compile_python_multi

    package = tmp_path / "park_api"
    inner = package / "inner"
    inner.mkdir(parents=True)
    entry = tmp_path / "entry.py"
    outer_init = package / "__init__.py"
    inner_init = inner / "__init__.py"
    leaf = inner / "leaf.py"
    parallel_ir = tmp_path / "parallel_reexport_park.ll"
    executable = tmp_path / "parallel_reexport_park"
    profile: dict = {}

    leaf.write_text(
        "import pcc.virtual_thread as vt\n"
        "def parked(value: int) -> int:\n"
        "    vt.yield_now()\n"
        "    return value + 1\n"
        "class Worker:\n"
        "    def leaf(self, value: int) -> int:\n"
        "        vt.yield_now()\n"
        "        return value + 1\n"
        "    def run(self, value: int) -> int:\n"
        "        return self.leaf(value) + 1\n",
        encoding="utf-8",
    )
    inner_init.write_text(
        "from .leaf import Worker, parked\n",
        encoding="utf-8",
    )
    outer_init.write_text(
        "from .inner import Worker, parked\n",
        encoding="utf-8",
    )
    entry.write_text(
        "import pcc.virtual_thread as vt\n"
        "from park_api import Worker, parked\n"
        "def through_function(value: int) -> int:\n"
        "    return parked(value)\n"
        "def through_method(value: int) -> int:\n"
        "    return Worker().run(value)\n"
        "def handler() -> int:\n"
        "    total = through_function(19) + through_method(19)\n"
        "    if through_function(1) < through_method(2):\n"
        "        return total\n"
        "    return 0\n"
        "def main() -> None:\n"
        "    thread = vt.spawn(handler)\n"
        "    vt.run(1, 32)\n"
        "    print(vt.result(thread))\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )

    compile_python_multi(
        [str(entry), str(outer_init), str(inner_init), str(leaf)],
        str(parallel_ir),
        module_names=[
            "entry",
            "park_api",
            "park_api.inner",
            "park_api.inner.leaf",
        ],
        entry_module="entry",
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
        emit_llvm_only=True,
        profile=profile,
    )
    counters = profile.get("counters", {})
    assert counters.get("multi_frontend_chunks") == 4
    assert counters.get("multi_frontend_ast_wire_enabled") == 1
    assert counters.get("multi_frontend_ast_wire_requested") == 0
    compile_python_multi(
        [str(entry), str(outer_init), str(inner_init), str(leaf)],
        str(executable),
        module_names=[
            "entry",
            "park_api",
            "park_api.inner",
            "park_api.inner.leaf",
        ],
        entry_module="entry",
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="llvm",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "41"


@pytest.mark.integration
def test_current_pcc1_self_no_libpython_transitive_park_resume(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    """The compiler under test is current pcc1, never host ``uv run pcc``."""
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        pytest.fail("current pcc1 is required for the may_park gateway gate")

    source = tmp_path / "pcc1_transitive_park.py"
    executable = tmp_path / "pcc1_transitive_park"
    source.write_text(TRANSITIVE_SOURCE, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_py_runtime_archive)
    built = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        [str(executable)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip().splitlines() == ["FINALLY", "4", "50"]


@pytest.mark.integration
@pytest.mark.parametrize("gc_backend", ("0", "1", "2", "3", "4"))
def test_current_pcc1_self_no_libpython_method_park_resume(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
    gc_backend: str,
) -> None:
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        pytest.fail("current pcc1 is required for the method may_park gate")

    source = tmp_path / ("pcc1_method_park_gc" + gc_backend + ".py")
    executable = tmp_path / ("pcc1_method_park_gc" + gc_backend)
    source.write_text(METHOD_SOURCE, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_py_runtime_archive)
    env["PCC_GC_BACKEND"] = gc_backend
    built = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        [str(executable)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "42"
