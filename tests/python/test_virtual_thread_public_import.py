"""The public package import must use the owned virtual-thread call route."""

from __future__ import annotations

import re
import subprocess
import textwrap

import pytest

from pcc.frontends.python.codegen.vthread_effect_analysis import (
    classify_vthread_park_boundaries,
    compute_vthread_may_park_functions,
    vthread_proven_suspension_module_alias,
)
from pcc.frontends.python.parser import parse
from pcc.frontends.python.py_ast import FuncDef


_IMPORTS = (
    ("from pcc import virtual_thread", "virtual_thread"),
    ("from pcc import virtual_thread as vt", "vt"),
    ("from pcc import i64, virtual_thread as vt, u64", "vt"),
    ("import pcc.virtual_thread as vt", "vt"),
)


def _analyze(source):
    module = parse(textwrap.dedent(source).lstrip(), "public_vthread_import.py")
    _ids, effects = compute_vthread_may_park_functions(module)
    return module, effects, classify_vthread_park_boundaries(module, effects)


@pytest.mark.parametrize("statement, alias", _IMPORTS)
@pytest.mark.parametrize("local", (False, True), ids=("module", "local"))
def test_public_import_proves_only_its_lexical_scope(statement, alias, local):
    source = (
        ("" if local else statement + "\n")
        + "def leaf() -> None:\n"
        + ("    " + statement + "\n" if local else "")
        + f"    {alias}.yield_now()\n"
        + "def caller() -> None:\n    leaf()\n"
        + f"def unrelated() -> None:\n    {alias}.yield_now()\n"
    )
    module, effects, rejected = _analyze(source)
    assert effects == ({"leaf", "caller"} if local else {"leaf", "caller", "unrelated"})
    assert rejected == {}
    funcs = {node.name: node for node in module.body if isinstance(node, FuncDef)}
    assert vthread_proven_suspension_module_alias(module, funcs["leaf"], alias)
    assert vthread_proven_suspension_module_alias(module, funcs["unrelated"], alias) is (not local)


@pytest.mark.parametrize("shadow", (
    "vt = other",
    "del vt",
    "from other import vt",
    "import other as vt",
    "for vt in values:\n    pass",
    "try:\n    pass\nexcept Exception as vt:\n    pass",
    "def vt():\n    pass",
    "class vt:\n    pass",
))
@pytest.mark.parametrize("local", (False, True), ids=("module", "local"))
def test_rebound_public_import_does_not_prove_native_suspension(shadow, local):
    source = "from pcc import virtual_thread as vt\n"
    if not local:
        source += shadow + "\n"
    source += "def worker(other, values) -> None:\n"
    if local:
        source += textwrap.indent(shadow, "    ") + "\n"
    source += "    vt.yield_now()\n"
    module, effects, rejected = _analyze(source)
    assert effects == set()
    assert rejected["worker"] == "dynamic binding shadows virtual-thread module alias: vt.yield_now"
    worker = next(node for node in module.body if isinstance(node, FuncDef) and node.name == "worker")
    assert not vthread_proven_suspension_module_alias(module, worker, "vt")


@pytest.mark.parametrize("body", (
    "def worker(vt) -> None:\n    vt.yield_now()\n",
    "def worker(other) -> None:\n    global vt\n    vt = other\n    vt.yield_now()\n",
    "def worker(other) -> None:\n    from pcc import virtual_thread as vt\n    vt = other\n    vt.yield_now()\n",
))
def test_parameter_and_global_rebinding_fail_closed(body):
    _module, effects, rejected = _analyze("from pcc import virtual_thread as vt\n" + body)
    assert effects == set()
    assert "dynamic binding shadows virtual-thread module alias" in rejected["worker"]


def test_exact_local_import_overrides_ambiguous_module_binding():
    _module, effects, rejected = _analyze('''
        from pcc import virtual_thread as vt
        vt = None
        def worker() -> None:
            from pcc import virtual_thread as vt
            vt.yield_now()
    ''')
    assert effects == {"worker"}
    assert rejected == {}


def test_module_value_alias_rebinding_remains_an_explicit_boundary():
    _module, effects, rejected = _analyze('''
        from pcc.virtual_thread import yield_now as pause
        pause = None
        def worker() -> None:
            pause()
    ''')
    assert effects == set()
    assert rejected["worker"] == "dynamic binding shadows virtual-thread primitive: pause"


@pytest.mark.parametrize("writer", (
    "def replace(other):\n    global vt\n    vt = other\n",
    "class Replacer:\n    def replace(self, other):\n        global vt\n        vt = other\n",
    "def outer():\n    def replace(other):\n        global vt\n        vt = other\n    return replace\n",
))
def test_global_write_in_another_scope_invalidates_module_alias(writer):
    _module, effects, rejected = _analyze(
        "from pcc import virtual_thread as vt\n" + writer
        + "def worker() -> None:\n    vt.yield_now()\n"
    )
    assert effects == set()
    assert rejected["worker"] == "dynamic binding shadows virtual-thread module alias: vt.yield_now"


def test_local_shadow_in_another_scope_keeps_module_alias():
    _module, effects, rejected = _analyze('''
        from pcc import virtual_thread as vt
        def local_shadow():
            vt = None
        class Holder:
            vt = None
        def worker() -> None:
            vt.yield_now()
    ''')
    assert effects == {"worker"}
    assert rejected == {}


def _compile_ir(tmp_path, source):
    from pcc.frontends.python.pipeline import compile_python

    path = tmp_path / "public_import.py"
    output = tmp_path / "public_import.ll"
    path.write_text(source, encoding="utf-8")
    compile_python(
        str(path), str(output), emit_llvm_only=True, backend="self",
        libpython_mode="off", ir_scaffold_mode="on",
    )
    return output.read_text(encoding="utf-8")


@pytest.mark.parametrize("statement, alias", _IMPORTS)
@pytest.mark.parametrize("local", (False, True), ids=("module", "local"))
def test_public_import_emits_owned_continuation_ir(tmp_path, statement, alias, local):
    source = (
        ("" if local else statement + "\n")
        + "def worker() -> None:\n"
        + ("    " + statement + "\n" if local else "")
        + f"    {alias}.yield_now()\n"
    )
    emitted = _compile_ir(tmp_path, source)
    has_continuation_marker = bool(re.search(r"call [^\n]*@py_gen_set_may_park\(", emitted))
    has_resume_state = bool(re.search(r"call [^\n]*@py_gen_set_state\([^\n]*i64 1\)", emitted))
    assert has_continuation_marker
    assert has_resume_state
    assert not re.search(r"call [^\n]*@py_cpy_import\(", emitted)
    assert "\\4E\\6F\\20\\6D\\6F\\64\\75\\6C\\65\\20\\6E\\61\\6D\\65\\64\\20\\27\\70\\63\\63\\27" not in emitted


def test_mixed_unknown_pcc_import_still_emits_import_error(tmp_path):
    emitted = _compile_ir(tmp_path, '''from pcc import virtual_thread as vt, no_such_export

def worker() -> None:
    vt.yield_now()
''')
    assert "\\4E\\6F\\20\\6D\\6F\\64\\75\\6C\\65\\20\\6E\\61\\6D\\65\\64\\20\\27\\70\\63\\63\\27" in emitted


@pytest.mark.parametrize("statement, alias", _IMPORTS[:3])
@pytest.mark.parametrize("local", (False, True), ids=("module", "local"))
def test_public_import_continuation_runs_natively(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive, statement, alias, local,
):
    from pcc.frontends.python.pipeline import compile_python

    # The threaded archive is admitted only for a threaded compilation.
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    path = tmp_path / "public_import.py"
    executable = tmp_path / "public_import"
    source = (
        "import pcc.virtual_thread as scheduler\n"
        + ("" if local else statement + "\n")
        + "def worker(value: int) -> int:\n"
        + ("    " + statement + "\n" if local else "")
        + "    saved: int = value + 1\n"
        + f"    {alias}.yield_now()\n"
        + "    return saved + 1\n"
        + "def main() -> None:\n"
        + "    thread = scheduler.spawn(worker, 40)\n"
        + "    scheduler.run(1, 32)\n"
        + "    print(scheduler.result(thread))\n"
        + "main()\n"
    )
    path.write_text(source, encoding="utf-8")
    compile_python(
        str(path), str(executable), backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive),
    )
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


@pytest.mark.parametrize("shadow", (
    "", "vt = other\n    pause = other",
    "del vt\n    del pause",
    "global vt, pause\n    vt = other\n    pause = other",
    "from other import vt, pause",
    "for vt, pause in values:\n        pass",
))
def test_cached_native_alias_proofs_preserve_lexical_binding_rules(shadow):
    from types import SimpleNamespace
    from pcc.frontends.python.codegen.native_modules import NativeModuleAliasMixin
    from pcc.frontends.python.codegen import vthread_effect_analysis as analysis

    module = parse(
        "from pcc import virtual_thread as vt\n"
        "from pcc.virtual_thread import yield_now as pause\n"
        "def worker(other, values):\n"
        + ("    " + shadow + "\n" if shadow else "")
        + "    vt.yield_now()\n    pause()\n",
        "cached_alias_rules.py",
    )
    fd = analysis._module_function_defs(module)[0]
    host = SimpleNamespace(
        ast_module=module, current_func_def=fd, env={}, _module_globals={},
        _native_builtin_module_aliases={"vt": "pcc.virtual_thread"},
        _native_builtin_value_aliases={"pause": "pcc.virtual_thread.yield_now"},
        _vthread_binding_cache={},
    )
    expected_module = None if shadow else "pcc.virtual_thread"
    expected_value = None if shadow else "pcc.virtual_thread.yield_now"
    for _ in range(2):
        assert NativeModuleAliasMixin._native_builtin_module_for_name(host, "vt") == expected_module
        assert NativeModuleAliasMixin._native_builtin_value_for_name(host, "pause") == expected_value
        for call in analysis._function_scope_nodes(fd):
            if not isinstance(call, analysis.Call):
                continue
            assert analysis.vthread_proven_suspension_call_key(
                module, fd, call, host._vthread_binding_cache,
            ) == expected_value


def test_cached_proofs_separate_module_and_function_replacements():
    from dataclasses import replace
    from pcc.frontends.python.codegen import vthread_effect_analysis as analysis

    module = parse(
        "from pcc import virtual_thread as vt\n"
        "from pcc.virtual_thread import yield_now as pause\n"
        "def worker():\n    vt.yield_now()\n    pause()\n",
        "same_name.py",
    )
    fd = analysis._module_function_defs(module)[0]
    rebound = parse("vt = None\npause = None\n", "same_name.py")
    changed_module = replace(module, body=module.body + rebound.body)
    changed_fd = replace(fd, body=rebound.body + fd.body)
    cache = {}
    for owner, function, expected in (
        (module, fd, True), (changed_module, fd, False),
        (module, changed_fd, False), (module, fd, True),
    ):
        assert analysis.vthread_proven_suspension_module_alias(
            owner, function, "vt", cache,
        ) is expected
        assert analysis.vthread_proven_value_alias(
            owner, function, "pause", "yield_now", cache,
        ) is expected
        assert analysis.vthread_proven_suspension_call_key(
            owner, function, fd.body[0].expr, cache,
        ) == ("pcc.virtual_thread.yield_now" if expected else None)


def test_cached_proofs_retain_identity_owners_until_cache_release():
    import gc
    import weakref
    from dataclasses import replace
    from pcc.frontends.python.codegen import vthread_effect_analysis as analysis

    module = parse(
        "from pcc import virtual_thread as vt\n"
        "def worker():\n    vt.yield_now()\n", "proof_lifetime.py",
    )
    # A lowered temporary function need not be contained in the module body.
    fd = replace(analysis._module_function_defs(module)[0])
    module_ref, function_ref = weakref.ref(module), weakref.ref(fd)
    cache = {}
    assert analysis.vthread_proven_suspension_module_alias(module, fd, "vt", cache)
    del module, fd
    gc.collect()
    assert module_ref() is not None
    assert function_ref() is not None
    cache.clear()
    gc.collect()
    assert module_ref() is None
    assert function_ref() is None


@pytest.mark.parametrize("explicit_module", (False, True))
def test_codegen_generation_replaces_preexisting_proof_cache(explicit_module):
    from copy import copy
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen import vthread_effect_analysis as analysis

    module = parse(
        "from pcc import virtual_thread as vt\n"
        "def worker() -> int:\n    return 1\n", "proof_generation.py",
    )
    fd = analysis._module_function_defs(module)[0]
    host = L1CodeGen(module, ir_scaffold_mode="on")
    other = L1CodeGen(module, ir_scaffold_mode="on")
    assert other._vthread_binding_cache is not host._vthread_binding_cache
    assert analysis.vthread_proven_suspension_module_alias(
        module, fd, "vt", host._vthread_binding_cache,
    )
    saved_cache = dict(host._vthread_binding_cache)
    copied_host = copy(host)
    previous_cache = host._vthread_binding_cache
    ir = host.generate(module if explicit_module else None)
    assert "define" in ir and "worker" in ir
    assert host._vthread_binding_cache is not previous_cache
    assert copied_host._vthread_binding_cache is previous_cache
    assert previous_cache == saved_cache
    assert other._vthread_binding_cache == {}
    assert analysis.vthread_proven_suspension_module_alias(
        host.ast_module, analysis._module_function_defs(host.ast_module)[0],
        "vt", host._vthread_binding_cache,
    )


def test_cached_nonlocal_aliases_remain_unproven():
    from pcc.frontends.python.codegen import vthread_effect_analysis as analysis

    module = parse(
        "from pcc import virtual_thread as vt\n"
        "from pcc.virtual_thread import yield_now as pause\n"
        "def outer(vt, pause):\n"
        "    def worker():\n"
        "        nonlocal vt, pause\n"
        "        vt.yield_now()\n"
        "        pause()\n"
        "    return worker\n", "nonlocal_proof.py",
    )
    fd = analysis._module_function_defs(module)[0].body[0]
    cache = {}
    for _ in range(2):
        assert not analysis.vthread_proven_suspension_module_alias(module, fd, "vt", cache)
        assert not analysis.vthread_proven_value_alias(module, fd, "pause", "yield_now", cache)
        assert analysis.vthread_proven_suspension_call_key(
            module, fd, fd.body[1].expr, cache,
        ) is None
