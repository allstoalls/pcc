"""Actual emitted constructor slot ownership and evaluation-order contracts."""
import re
import pytest
from test_shared_call_binding import _emit
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module

SOURCES = [
    'def target(value=7):\n    "doc"\n    return value\npublished = target\n',
    'def outer(capture):\n    def inner(value=3):\n        return capture + value\n    return inner\n',
    'def outer(capture):\n    yield capture\n    def inner(value=3):\n        return capture + value\n    yield inner\n',
    'def default():\n    return 3\ndef decorate(fn):\n    return fn\n@decorate\ndef target(value=default()):\n    return value\n',
]

@pytest.mark.parametrize('source', SOURCES)
def test_construction_status_edges_own_output_and_parse(source):
    text = _emit(source)
    verify_parsed_module(parse_self_backend_module(text))
    calls = list(re.finditer(
        r'(?m)^\s*(%\S+) = call i64[^\n]*@(?:py_func_new_signature_slots|py_func_new_static_def)\(([^\n]+)\)\n',
        text,
    ))
    assert calls
    for call in calls:
        tail = text[call.end():]
        # Persistent generator roots must become owned immediately, before
        # status checking; lexical roots have no conditional ownership flag.
        first = tail.splitlines()[0].strip()
        assert first.startswith(('store i1 1, ptr ', '%')), first
        check = re.search(r'icmp slt i64 ' + re.escape(call.group(1)) + r', 0', tail)
        assert check is not None
        prefix = tail[:check.start()]
        assert 'call ' not in prefix and 'br ' not in prefix
    assert not re.search(r'call [^\n]*@py_func_new_named\(', text)
    # The static constructor initializes metadata inside the runtime.
    assert '@py_func_init_metadata_slots(' in text or '@py_func_new_static_def(' in text


def test_static_definitions_use_one_runtime_constructor():
    # A capture-free def with constant defaults and a literal docstring is
    # built by one call carrying its static signature, docstring and
    # definition cache; captures or an evaluated default keep the inline
    # route with its per-step roots.
    text = _emit(SOURCES[0])
    call = re.search(r'call i64[^\n]*@py_func_new_static_def\(([^\n]+)\)', text)
    assert call is not None
    args = call.group(1)
    descriptor = re.search(r'ptr (%static\.agg\.func\.sig\.[^ ,]+)', args)
    assert descriptor is not None, args
    definition = re.search(r'(?m)^\s*' + re.escape(descriptor.group(1)) + r' = ([^\n]+)', text)
    assert definition is not None and '@.pcc.static.agg.func.sig.' in definition.group(1), args
    assert '.pystr.obj.' in args, args
    assert '@__pcc_native_func_value_cache_' in args, args
    assert not re.search(r'call i64[^\n]*@py_func_new_signature_slots\(', text)
    for source in (SOURCES[1], SOURCES[3]):
        inline = _emit(source)
        assert re.search(r'call i64[^\n]*@py_func_new_signature_slots\(', inline)


def test_constructor_persistent_flag_is_set_before_status_failure_edge():
    text = _emit(SOURCES[2])
    assert re.search(r'call i64[^\n]*@py_func_new_signature_slots\([^\n]*\)\n\s+store i1 1, ptr %[^\n]*owned', text)


def test_constructor_small_stackmap_gate():
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    prepared = prepare_module_for_target(_emit(SOURCES[1]), aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)


def _assert_inputs_retire_before_metadata(text):
    # Source evaluation still uses the original lowering. Examine actual IR
    # around each suffix: source slots retire before metadata allocation.
    aliases = dict(re.findall(r'(%[^ ]+) = bitcast ptr (%[^ ]+) to ptr', text))
    def resolve(value):
        while value in aliases:
            value = aliases[value]
        return value
    calls = list(re.finditer(r'call i64[^\n]*@py_func_new_signature_slots\(ptr (%[^ ,]+), ptr (%[^ ,]+),[^\n]*\)', text))
    assert calls, 'missing callable constructor calls'
    for call in calls:
        tail = text[call.end():]
        metadata = re.search(r'call i64[^\n]*@py_func_init_metadata_slots\(', tail)
        assert metadata
        retired = {resolve(v) for v in re.findall(r'call void[^\n]*@pcc_gc_store_root\(ptr (%[^ ,]+), ptr null\)', tail[:metadata.start()])}
        assert {resolve(call.group(1)), resolve(call.group(2))} <= retired


def test_inputs_retire_before_metadata():
    _assert_inputs_retire_before_metadata(_emit(SOURCES[3]))


@pytest.mark.parametrize('text', [
    '',
    'declare i64 @py_func_new_signature_slots(ptr, ptr, ptr, ptr, ptr)\n',
])
def test_input_retirement_assertion_rejects_missing_constructors(text):
    with pytest.raises(AssertionError, match='missing callable constructor calls'):
        _assert_inputs_retire_before_metadata(text)


def test_actual_runtime_helper_publishes_new_before_any_callback(tmp_path):
    import ast
    from pathlib import Path
    from pcc.frontends.python.pipeline import compile_python
    root=tmp_path; path=Path(__file__).resolve().parents[2]/'pcc/runtime/py/py_func.py'; source=path.read_text(); tree=ast.parse(source)
    selected=[]
    for n in tree.body:
        if isinstance(n,(ast.Import,ast.ImportFrom)) or isinstance(n,ast.Assign) and not any(isinstance(t,ast.Name) and t.id.startswith('_FUNC_METADATA') for t in n.targets):
            if n.lineno < 141 or any(isinstance(t, ast.Name) and t.id == 'py_func_new_named_raw' for t in getattr(n,'targets',())) or any(isinstance(t,ast.Name) and t.id.startswith('_FUNC_CONSTRUCTION') for t in getattr(n,'targets',())): selected.append(n)
        if isinstance(n,ast.FunctionDef) and (n.name.startswith('_func_construction') or n.name in ('py_func_new_signature_slots','py_func_new_named','py_func_new_bound')): selected.append(n)
    from pcc.runtime.py import py_abi_constants as abi
    selected = [n for n in selected if not (isinstance(n, ast.ImportFrom) and n.module == 'pcc.runtime.py.py_abi_constants')]
    selected[1:1] = [ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=ast.Constant(value=getattr(abi,name))) for name in dir(abi) if name.isupper() and type(getattr(abi,name)) is int]
    selected = [ast.fix_missing_locations(n) for n in selected]
    probe=root/'constructor_runtime_probe.py'; probe.write_text(ast.unparse(ast.Module(body=selected,type_ignores=[])))
    compile_python(str(probe),str(root/'constructor_runtime_probe.ll'),emit_llvm_only=True,ir_scaffold_mode='on',libpython_mode='off')
    text = (root/'constructor_runtime_probe.ll').read_text()
    verify_parsed_module(parse_self_backend_module(text))
    body = re.search(r'^define [^\n]*@py_func_new_signature_slots\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    for producer in ('py_tuple_new', 'py_func_new_named'):
        call = re.search(r'(%[^ ]+) = call ptr[^\n]*@' + producer + r'\([^\n]*\)\n', body)
        assert call
        first = body[call.end():].splitlines()[0].strip()
        assert first.startswith('store ptr ' + call.group(1) + ', ptr '), first
    assert 'call.ret.root' not in body
