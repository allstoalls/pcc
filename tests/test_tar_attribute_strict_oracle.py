"""Source/IR controls and one bounded strict host-IR check; no native execution."""

import ast
import hashlib
import json
from pathlib import Path
import re
import textwrap

import pytest

from tests import tar_attribute_strict_oracle as oracle
from tests.integration.test_worker_size_prior_closed_world import require_definition


class _Pool:
    def __init__(self):
        self.declarations = ''
        self.names = {}

    def name(self, value):
        if value not in self.names:
            name = f'@".literal.{len(self.names)}"'
            data = ''.join('\\' + format(byte, '02X') for byte in value.encode()) + '\\00'
            self.declarations += f'{name} = internal constant [{len(value.encode()) + 1} x i8] c"{data}"\n'
            self.names[value] = name
        return self.names[value]


def _function(symbol, body, *, method=False, adapter=False):
    result, args = ('ptr', 'ptr %self, ptr %intrinsic, ptr %expr') if method else ('void', '')
    if adapter:
        result, args = 'ptr', 'ptr %captures, ptr %args'
    return f'define external {result} @{symbol}({args}) {{\n{body}  ret {result}' + (
        ' null\n}\n' if result == 'ptr' else '\n}\n'
    )


def _source():
    diagnostics = oracle._MUTATION_DIAGNOSTICS
    body = ''.join(f'if intrinsic == {name!r}:\n    raise NotImplementedError({message!r})\n'
                   for name, message in zip(('open_file', 'rename_file', 'chmod_file'), diagnostics[:3]))
    body += textwrap.dedent(oracle._UTIME_BRANCH).strip() + '\n'
    body += f'if intrinsic == "sync_file":\n    raise NotImplementedError({diagnostics[4]!r})\n'
    body += f'raise NotImplementedError({diagnostics[5]!r})\n'
    dispatch = textwrap.dedent(oracle._WINDOWS_BRANCH).strip() + '\n'
    dispatch += 'raise NotImplementedError(f"unknown pcc.unsafe intrinsic {intrinsic!r}")\n'
    return ('class UnsafeIntrinsicMixin:\n'
            '    _UNSAFE_INTRINSICS = UNSAFE_INTRINSICS\n'
            '    def _is_unsafe_intrinsic(self, name: str) -> bool:\n'
            '        return name in self._UNSAFE_INTRINSICS\n'
            '    def _emit_unsafe_file_mutation(self, intrinsic: str, expr: Call) -> ir.Value:\n'
            + textwrap.indent(body, '        ')
            + '    def _emit_unsafe_intrinsic_call(self, intrinsic: str, expr: Call) -> ir.Value:\n'
            + textwrap.indent(dispatch, '        '))


def _method_ir(pool):
    body = '  br i1 true, label %work, label %bad\nwork:\n'
    callees = ('user_pcc_ir_ir_IRBuilder_call2', 'user_pcc_ir_ir_IRBuilder_sext',
               oracle._METHOD_PREFIX + '_unsafe_expect_arity', oracle._METHOD_PREFIX + '_unsafe_ptr_arg',
               oracle._METHOD_PREFIX + '_target_sys_platform_text',
               'user_pcc_frontends_python_codegen_ir_decl_helpers_IrDeclHelperMixin__declare_external_function')
    body += ''.join(f'  call ptr @{callee}()\n' for callee in callees)
    for literal in ('utime_file', 'utime', 'unsafe.utime_file.i32', 'unsafe.utime_file'):
        body += f'  call void @literal_use(ptr {pool.name(literal)})\n'
    for literal in oracle._MUTATION_DIAGNOSTICS:
        body += f'  call ptr @py_exc_new(i64 11, ptr {pool.name(literal)})\n'
    mutation = _function(oracle.MUTATION_METHOD, body, method=True)
    body = '  br i1 true, label %work, label %bad\nwork:\n'
    body += '  call ptr @py_exc_new_with_value(i64 11, ptr null)\n'
    for callee in (oracle.MUTATION_METHOD,
                   'user_pcc_frontends_python_codegen_windows_platform_handles',
                   'user_pcc_frontends_python_codegen_windows_platform_emit'):
        body += f'  call ptr @{callee}()\n'
    for literal in ('win32', 'unknown pcc.unsafe intrinsic '):
        body += f'  call void @literal_use(ptr {pool.name(literal)})\n'
    dispatch = _function(oracle.DISPATCH_METHOD, body, method=True)
    adapters = []
    class_global = '.class.' + oracle._UNSAFE.replace('.', '_') + '.UnsafeIntrinsicMixin'
    pool.declarations += f'@"{class_global}" = global ptr null\n'
    class_name = pool.name('UnsafeIntrinsicMixin')
    bindings = (f'  %class = call ptr @py_class_new(ptr {class_name}, ptr null, i32 0, ptr null, i32 0)\n'
                f'  store ptr %class, ptr @"{class_global}"\n'
                '  %capture_tuple = call ptr @py_tuple_new(i64 0)\n'
                '  store ptr %capture_tuple, ptr %captures\n'
                '  %signature_tuple = call ptr @py_tuple_new(i64 5)\n'
                '  store ptr %signature_tuple, ptr %signature\n'
                f'  call void @py_tuple_set_item(ptr %signature_tuple, i64 0, ptr {pool.name("__pcc_func_signature_v1__")})\n')
    for index, symbol in enumerate((oracle.MUTATION_METHOD, oracle.DISPATCH_METHOD)):
        adapter = symbol + '_method_native_adapter'
        adapters.append(_function(adapter, f'  call ptr @{symbol}()\n', adapter=True))
        name = pool.name(symbol[len(oracle._METHOD_PREFIX):])
        bindings += f'  %status{index} = call i64 @py_func_new_signature_slots(ptr %captures, ptr %signature, ptr @{adapter}, ptr {name}, ptr %output{index})\n'
        bindings += f'  %callable{index} = call ptr @pcc_gc_take_pinned_slot(ptr %output{index}, i64 0)\n'
        bindings += f'  store ptr %callable{index}, ptr %publication{index}\n'
        bindings += f'  %published_method{index} = load ptr, ptr %publication{index}\n'
        bindings += f'  call void @py_class_add_method(ptr %class, ptr {name}, ptr %published_method{index})\n'
        bindings += f'  call i64 @py_class_setattr_raw(ptr %class, ptr {name}, ptr %published_method{index})\n'
    return mutation + dispatch + ''.join(adapters), bindings


def _dynamic(pool, attribute, *, method=False, slots=False, suffix=''):
    name = pool.name(attribute)
    body = f'  %attribute{suffix} = getelementptr inbounds [1 x i8], ptr {name}, i32 0, i32 0\n'
    if method:
        body += f'  %callable{suffix} = call ptr @py_obj_load_method(ptr %owner, ptr %attribute{suffix}, ptr %self_slot)\n'
        body += f'  call ptr @py_obj_call_method(ptr %callable{suffix}, ptr %self, ptr %args)\n'
    else:
        body += f'  %callable{suffix} = call ptr @py_obj_getattr(ptr %owner, ptr %attribute{suffix})\n'
        if slots:
            body += f'  store ptr %callable{suffix}, ptr %root{suffix}\n'
            body += f'  call i64 @pcc_gc_root_copy_lease(ptr %copy{suffix}, ptr %root{suffix})\n'
            body += f'  call i64 @py_obj_call_slots(ptr %copy{suffix}, ptr %args, ptr %kwargs, ptr %out)\n'
        else:
            body += f'  call ptr @py_obj_call(ptr %callable{suffix}, ptr %args, ptr %kwargs)\n'
    return body


def _import(pool, module, table, index):
    module_name, table_name = pool.name(module), pool.name(table)
    return (f'  %module{index} = call ptr @py_compiled_module_import_by_name(ptr {module_name})\n'
            f'  store ptr %module{index}, ptr %module_root{index}\n'
            f'  %receiver{index} = call ptr @pcc_gc_load_ptr(ptr null, ptr %module_root{index})\n'
            f'  %member{index} = call ptr @py_obj_getattr(ptr %receiver{index}, ptr {table_name})\n'
            f'  store ptr %member{index}, ptr %member_root{index}\n'
            f'  call i64 @pcc_gc_root_copy_borrowed_lease(ptr %local{index}, ptr %member_root{index})\n'
            f'  %table{index} = load ptr, ptr %local{index}\n'
            f'  call void @py_dict_update(ptr %destination, ptr %table{index})\n')


def _fixture(name):
    pool = _Pool()
    tables = {
        oracle.TAR_COMPILER_MODULES[0]: (('_KERNEL32', 'py_set_from_iterable'),),
        oracle.CONSTANT_ONLY_MODULE: (('PLATFORM_MACHINE_ABIS', 'py_static_aggregate_build'),),
        oracle._UNSAFE: (('UNSAFE_INTRINSICS', 'py_set_new'), ('_UNSAFE_INTRINSIC_FAMILIES', 'py_static_aggregate_build')),
        oracle._WINDOWS: (('_SIGNATURES', 'py_static_aggregate_build'),),
        oracle.TAR_COMPILER_MODULES[5]: (('_UNSAFE_INTRINSIC_RETURN_TYPES', 'py_dict_new_presized'),),
    }
    declarations, initialization = '', ''
    for index, (table, constructor) in enumerate(tables.get(name, ())):
        slot = '.modvar.' + name.replace('.', '_') + '.' + table
        declarations += f'@"{slot}" = global ptr null\n'
        if name == oracle._UNSAFE and table == 'UNSAFE_INTRINSICS':
            initialization += ('  %registry_left = call ptr @py_set_new()\n'
                               '  %registry_right = call ptr @py_set_from_iterable(ptr %metadata)\n'
                               '  %table0 = call ptr @py_obj_or(ptr %registry_left, ptr %registry_right)\n')
        else:
            initialization += f'  %table{index} = call ptr @{constructor}()\n'
        initialization += f'  call void @pcc_gc_store_root(ptr @"{slot}", ptr %table{index})\n'
        initialization += f'  %published{index} = load ptr, ptr @"{slot}"\n'
        initialization += f'  call void @py_module_attr_set(ptr {pool.name(name)}, ptr {pool.name(table)}, ptr %published{index})\n'
    section = ''
    if name == oracle._UNSAFE:
        methods, bindings = _method_ir(pool)
        section += methods
        initialization += bindings
        registry = pool.name('_UNSAFE_INTRINSICS')
        slot = '.modvar.' + name.replace('.', '_') + '.UNSAFE_INTRINSICS'
        initialization += f'  %registry = load ptr, ptr @"{slot}"\n'
        initialization += f'  call i64 @py_class_setattr_raw(ptr %class, ptr {registry}, ptr %registry)\n'
        body = (f'  %registry = call ptr @py_obj_getattr(ptr %self, ptr {registry})\n'
                '  call i64 @py_obj_contains(ptr %registry, ptr %name)\n')
        section += _function(oracle._METHOD_PREFIX + '_is_unsafe_intrinsic', body)
    if tables.get(name):
        section += _function('_pcc_py_module_top_' + name.replace('.', '_'), initialization)
    consumers = {
        oracle.TAR_COMPILER_MODULES[0]: (('system_dll', '_KERNEL32'),),
        oracle._UNSAFE: (('_unsafe_intrinsic_family', '_UNSAFE_INTRINSIC_FAMILIES'),),
        oracle._WINDOWS: (('handles', '_SIGNATURES'), ('emit', '_SIGNATURES')),
        oracle.TAR_COMPILER_MODULES[5]: (('_unsafe_intrinsic_return_type', '_UNSAFE_INTRINSIC_RETURN_TYPES'),),
    }
    for function, table in consumers.get(name, ()):
        slot = '.modvar.' + name.replace('.', '_') + '.' + table
        body = f'  %value = load ptr, ptr @"{slot}"\n  call i64 @py_obj_contains(ptr %value, ptr %key)\n'
        if name == oracle._WINDOWS and function == 'emit':
            for literal in ('pcc_win_', 'builder'):
                body += f'  call void @literal_use(ptr {pool.name(literal)})\n'
            body += _dynamic(pool, '_declare_external_function', method=True, suffix='declare')
            body += f'  %builder = call ptr @py_obj_getattr(ptr %owner, ptr {pool.name("builder")})\n'
            body += _dynamic(pool, 'call', slots=True, suffix='call').replace('ptr %owner,', 'ptr %builder,')
        emitted = _function('user_' + name.replace('.', '_') + '_' + function, body,
                            method=name == oracle._WINDOWS and function == 'emit')
        if name == oracle._WINDOWS and function == 'emit':
            emitted = emitted.replace('ptr %self, ptr %intrinsic, ptr %expr', 'ptr %owner, ptr %intrinsic, ptr %expr')
        section += emitted
    remaining = {}
    if name in (oracle.CONSTANT_ONLY_MODULE, oracle._FREESTANDING):
        consumer_pool = _Pool()
        body = _import(consumer_pool, oracle.CONSTANT_ONLY_MODULE, 'PLATFORM_MACHINE_ABIS', 0)
        body += _import(consumer_pool, oracle._WINDOWS, '_SIGNATURES', 1)
        for literal in ('utime_file', 'utime'):
            body += f'  call void @literal_use(ptr {consumer_pool.name(literal)})\n'
        consumer = consumer_pool.declarations + _function('user_' + oracle._FREESTANDING.replace('.', '_')
                                                         + '_freestanding_allowed_external_symbols', body)
        if name == oracle._FREESTANDING:
            section = consumer
        else:
            remaining[oracle._FREESTANDING] = consumer
    return declarations + pool.declarations + section, remaining


@pytest.mark.parametrize('name', oracle.TAR_COMPILER_MODULES)
def test_six_strict_compiler_modules_require_real_tables_and_consumers(name, record_property):
    section, remaining = _fixture(name)
    required = oracle.require_tar_attribute_module(name, _source(), section, remaining, require_definition)
    assert bool(required) == (name != oracle._FREESTANDING)
    if name == oracle._UNSAFE:
        assert oracle.MUTATION_METHOD in required and oracle.DISPATCH_METHOD in required
        assert len(required) == 6
        _check_static_unsafe_method_signatures_keep_real_class_binding()
        checked = ["static-signature"]
        for alias in ("bitcast", "zero_gep"):
            _check_method_binding_preserves_exact_class_root_address_aliases(alias)
            checked.append("class-root-" + alias)
        for damage in _STATIC_METHOD_BINDING_DAMAGES:
            _check_static_unsafe_method_binding_rejects_malformed_or_detached_values(damage)
            checked.append("reject-" + damage)
        assert len(checked) == 3 + len(_STATIC_METHOD_BINDING_DAMAGES)
        record_property("static_method_binding_controls", json.dumps(checked))
        section, body = _static_constructor_binding_fixture()
        oracle.require_tar_unsafe_methods(_source(), section, body, require_definition)
        constructor_checks = ["static-constructor"]
        for alias in ("bitcast", "zero_gep"):
            _check_method_binding_preserves_exact_class_root_address_aliases(alias, constructor=True)
            constructor_checks.append("class-root-" + alias)
        for damage in _STATIC_CONSTRUCTOR_SHARED_DAMAGES:
            _check_static_unsafe_method_binding_rejects_malformed_or_detached_values(damage, constructor=True)
            constructor_checks.append("reject-" + damage)
        for damage in _STATIC_CONSTRUCTOR_DAMAGES:
            _check_static_constructor_binding_rejects_damage(damage)
            constructor_checks.append("reject-" + damage)
        assert len(constructor_checks) == 3 + len(_STATIC_CONSTRUCTOR_SHARED_DAMAGES) + len(_STATIC_CONSTRUCTOR_DAMAGES)
        record_property("static_constructor_binding_controls", json.dumps(constructor_checks))


@pytest.mark.parametrize('name', (
    oracle.TAR_COMPILER_MODULES[0], oracle.CONSTANT_ONLY_MODULE, oracle._UNSAFE,
    oracle._WINDOWS, oracle.TAR_COMPILER_MODULES[5],
))
@pytest.mark.parametrize('damage', ('constructor', 'publication', 'consumer', 'initializer', 'detached_value'))
def test_table_oracle_rejects_dormant_or_unbound_tables(name, damage):
    section, remaining = _fixture(name)
    if damage == 'constructor':
        for call in ('py_dict_new_presized', 'py_set_from_iterable', 'py_set_new', 'py_static_aggregate_build'):
            section = section.replace('@' + call + '(', '@unowned_constructor(')
    elif damage == 'publication':
        section = section.replace('@py_module_attr_set(', '@not_a_publication(')
    elif damage == 'initializer':
        section = section.replace('define external void @_pcc_py_module_top_', 'define external void @unrelated_initializer_')
    elif damage == 'detached_value':
        section = section.replace(', ptr %table0)', ', ptr %unrelated)')
    elif name == oracle.CONSTANT_ONLY_MODULE:
        remaining[oracle._FREESTANDING] = remaining[oracle._FREESTANDING].replace('ptr %receiver0,', 'ptr %unrelated,')
    else:
        section = section.replace('ptr %value, ptr %key', 'ptr %unrelated, ptr %key')
    with pytest.raises(AssertionError):
        oracle.require_tar_attribute_module(name, _source(), section, remaining, require_definition)


@pytest.mark.parametrize('damage', (
    'left_constructor', 'right_constructor', 'missing_left', 'missing_right',
    'constructor_arity', 'constructor_return_type', 'constructor_argument_type',
    'left_operand', 'right_operand', 'ambiguous_operand', 'unknown_root_value',
    'same_constructor', 'wrong_operator', 'wrong_callee', 'wrong_arity',
    'wrong_return_type', 'wrong_argument_type', 'detached_result', 'ignored_union',
    'substituted_global', 'publication', 'class_binding', 'consumer',
))
def test_unsafe_registry_requires_its_fresh_owned_set_union(damage):
    section, remaining = _fixture(oracle._UNSAFE)
    replacements = {
        'left_constructor': ('%registry_left = call ptr @py_set_new()',
                             '%registry_left = call ptr @py_dict_new()'),
        'right_constructor': ('%registry_right = call ptr @py_set_from_iterable(ptr %metadata)',
                              '%registry_right = call ptr @py_dict_new()'),
        'missing_left': ('%registry_left = call ptr @py_set_new()', ''),
        'missing_right': ('%registry_right = call ptr @py_set_from_iterable(ptr %metadata)', ''),
        'constructor_arity': ('@py_set_from_iterable(ptr %metadata)', '@py_set_from_iterable()'),
        'constructor_return_type': ('%registry_right = call ptr @py_set_from_iterable(',
                                    '%registry_right = call i64 @py_set_from_iterable('),
        'constructor_argument_type': ('@py_set_from_iterable(ptr %metadata)',
                                      '@py_set_from_iterable(i64 %metadata)'),
        'left_operand': ('@py_obj_or(ptr %registry_left,', '@py_obj_or(ptr %unrelated,'),
        'right_operand': ('@py_obj_or(ptr %registry_left, ptr %registry_right)',
                          '@py_obj_or(ptr %registry_left, ptr %unrelated)'),
        'ambiguous_operand': ('%table0 = call ptr @py_obj_or(ptr %registry_left, ptr %registry_right)',
                              'store ptr %registry_left, ptr %mixed\n'
                              '  store ptr %registry_right, ptr %mixed\n'
                              '  %ambiguous = load ptr, ptr %mixed\n'
                              '  %table0 = call ptr @py_obj_or(ptr %ambiguous, ptr %registry_right)'),
        'unknown_root_value': ('%table0 = call ptr @py_obj_or(ptr %registry_left, ptr %registry_right)',
                               'store ptr %registry_left, ptr %mixed\n'
                               '  store ptr %unrelated, ptr %mixed\n'
                               '  %ambiguous = load ptr, ptr %mixed\n'
                               '  %table0 = call ptr @py_obj_or(ptr %ambiguous, ptr %registry_right)'),
        'same_constructor': ('@py_obj_or(ptr %registry_left, ptr %registry_right)',
                             '@py_obj_or(ptr %registry_left, ptr %registry_left)'),
        'wrong_operator': ('@py_obj_or(', '@py_obj_xor('),
        'wrong_callee': ('@py_obj_or(', '@unrelated('),
        'wrong_arity': ('@py_obj_or(ptr %registry_left, ptr %registry_right)',
                        '@py_obj_or(ptr %registry_left)'),
        'wrong_return_type': ('%table0 = call ptr @py_obj_or(', '%table0 = call i64 @py_obj_or('),
        'wrong_argument_type': ('@py_obj_or(ptr %registry_left,', '@py_obj_or(i64 %registry_left,'),
        'detached_result': (', ptr %table0)', ', ptr %unrelated)'),
        'ignored_union': (', ptr %table0)', ', ptr %registry_left)'),
        'substituted_global': ('ptr @".modvar.' + oracle._UNSAFE.replace('.', '_') + '.UNSAFE_INTRINSICS", ptr %table0)',
                               'ptr %unrelated_global, ptr %table0)'),
        'publication': (', ptr %published0)', ', ptr %unrelated)'),
        'class_binding': (', ptr %registry)', ', ptr %unrelated)'),
        'consumer': ('@py_obj_contains(ptr %registry,', '@py_obj_contains(ptr %unrelated,'),
    }
    old, new = replacements[damage]
    assert old in section
    section = section.replace(old, new)
    with pytest.raises(AssertionError):
        oracle.require_tar_attribute_module(oracle._UNSAFE, _source(), section, remaining, require_definition)


def test_union_result_does_not_inherit_operand_identity():
    flow = oracle._Flow('  %result = call ptr @py_obj_or(ptr %left, ptr %right)\n')
    assert not flow.reaches('%result', {'%left', '%right'})


def test_other_table_initializers_do_not_admit_set_union_results():
    name = oracle.TAR_COMPILER_MODULES[0]
    section, remaining = _fixture(name)
    section = section.replace(
        '%table0 = call ptr @py_set_from_iterable()',
        '%left = call ptr @py_set_new()\n'
        '  %right = call ptr @py_set_new()\n'
        '  %table0 = call ptr @py_obj_or(ptr %left, ptr %right)',
    )
    with pytest.raises(AssertionError):
        oracle.require_tar_attribute_module(name, _source(), section, remaining, require_definition)


@pytest.mark.parametrize('old,new', (
    ('("darwin", "linux")', '("darwin", "win32")'),
    ('self._unsafe_expect_arity(intrinsic, expr, 2)', 'self._unsafe_expect_arity(intrinsic, expr, 1)'),
    ('[_CSTR, _CSTR]', '[_CSTR]'),
    ('self.builder.sext(raw, _I64', 'self.builder.zext(raw, _I64'),
    ('== "win32"', '== "linux"'),
    ('return emit(self, intrinsic, expr)', 'return None'),
    ('unknown pcc.unsafe intrinsic', 'silently unsupported'),
    ('if intrinsic == "utime_file":', 'raise NotImplementedError(intrinsic)\n        if intrinsic == "utime_file":'),
))
def test_method_source_oracle_rejects_changed_supported_or_diagnostic_contract(old, new):
    with pytest.raises(AssertionError):
        oracle._method_source(_source().replace(old, new))


@pytest.mark.parametrize('damage', (
    'call2', 'sext', 'windows_emit', 'signature', 'literal', 'binding', 'diagnostic_count', 'adapter',
    'py_cpy', 'strict_stub', 'extra_dynamic_diagnostic', 'adapter_signature', 'factory_argument',
    'class_setter', 'class_value', 'callable_value', 'signature_value', 'capture_value',
))
def test_method_ir_oracle_rejects_stub_signature_binding_and_callee_replacements(damage):
    pool = _Pool()
    section, binding = _method_ir(pool)
    section = pool.declarations + section
    replacements = {
        'call2': ('@user_pcc_ir_ir_IRBuilder_call2(', '@unrelated('),
        'sext': ('@user_pcc_ir_ir_IRBuilder_sext(', '@unrelated('),
        'windows_emit': ('@user_pcc_frontends_python_codegen_windows_platform_emit(', '@unrelated('),
        'signature': ('ptr %self, ptr %intrinsic, ptr %expr', 'i64 %self, ptr %intrinsic, ptr %expr'),
        'literal': ('call void @literal_use(ptr ' + pool.name('utime_file') + ')', 'call void @unrelated()'),
        'diagnostic_count': ('@py_exc_new(i64 11,', '@py_exc_new(i64 3,'),
        'adapter': ('call ptr @' + oracle.MUTATION_METHOD + '()', 'call ptr @unrelated()'),
        'py_cpy': ('@user_pcc_ir_ir_IRBuilder_call2(', '@py_cpy_call('),
        'strict_stub': ('@user_pcc_ir_ir_IRBuilder_call2(', '@strict.nolib.stub('),
        'extra_dynamic_diagnostic': ('work:\n', 'work:\n  call ptr @py_exc_new_with_value(i64 11, ptr null)\n'),
        'adapter_signature': ('ptr %captures, ptr %args', 'ptr %captures'),
    }
    if damage == 'binding':
        binding = binding.replace('@py_func_new_signature_slots(', '@unrelated(')
    elif damage == 'factory_argument':
        binding = binding.replace('ptr %signature, ptr @', 'ptr %signature, ptr %wrong, ptr @')
    elif damage == 'signature_value':
        binding = binding.replace('store ptr %signature_tuple,', 'store ptr %wrong,')
    elif damage == 'capture_value':
        binding = binding.replace('store ptr %capture_tuple,', 'store ptr %wrong,')
    elif damage == 'class_setter':
        binding = binding.replace('@py_class_add_method(', '@unrelated(')
    elif damage == 'class_value':
        binding = binding.replace('@py_class_add_method(ptr %class,', '@py_class_add_method(ptr %other_class,')
    elif damage == 'callable_value':
        binding = binding.replace(', ptr %published_method0)', ', ptr %unrelated)')
    else:
        old, new = replacements[damage]
        section = section.replace(old, new, 1)
    with pytest.raises(AssertionError):
        oracle.require_tar_unsafe_methods(_source(), section, binding, require_definition)


@pytest.mark.parametrize('route', ('direct', 'method', 'slots'))
@pytest.mark.parametrize('damage', ('none', 'wrong_attribute', 'detached_callable', 'detached_receiver'))
def test_dynamic_calls_require_the_named_attribute_callable_and_receiver(route, damage):
    pool = _Pool()
    body = _dynamic(pool, 'call', method=route == 'method', slots=route == 'slots')
    pool.name('other')
    if damage == 'wrong_attribute':
        body = body.replace(pool.name('call'), pool.name('other'))
    elif damage == 'detached_callable':
        body = body.replace('ptr %callable,', 'ptr %unrelated,')
        body = body.replace('store ptr %callable,', 'store ptr %unrelated,')
    elif damage == 'detached_receiver':
        body = body.replace('ptr %owner,', 'ptr %unrelated,')
    if damage == 'none':
        oracle._dynamic_attribute_call(pool.declarations, body, 'call', receiver={'%owner'})
    else:
        with pytest.raises(AssertionError):
            oracle._dynamic_attribute_call(pool.declarations, body, 'call', receiver={'%owner'})


def test_new_oracle_does_not_change_previous_module_requirements():
    assert oracle.require_tar_attribute_module('pcc.backend.elf_x86_64', '', '', {}, require_definition) == []


def test_original_symbol_oracle_still_rejects_notimplemented_stub():
    with pytest.raises(AssertionError):
        require_definition(_function('old_symbol', '  call ptr @py_exc_new(i64 11, ptr null)\n'), 'old_symbol')


def _current_printer(section, *, holder='constant', alias='bitcast', retained='%value'):
    """Rewrite a fixture into the shapes the current IR printer emits.

    Calls carry their function type, module roots are stored through a
    bitcast slot, consumers retain the loaded table before using it and
    literal objects reach their users through constant static item arrays.
    """
    count = iter(range(1_000_000))

    def store(match):
        index = next(count)
        cast = (f'bitcast ptr {match[1]} to ptr' if alias == 'bitcast'
                else f'getelementptr inbounds ptr, ptr {match[1]}, i64 1')
        return (f'%root.slot{index} = {cast}\n'
                f'  call void @pcc_gc_store_root(ptr %root.slot{index}, ptr {match[2]})')

    section = re.sub(r'call void @pcc_gc_store_root\(ptr (@"[^"]+"), ptr (%\w+)\)', store, section)
    section = section.replace(
        'call i64 @py_obj_contains(ptr %value, ptr %key)',
        f'%retained = call ptr @pcc_gc_retain(ptr {retained})\n'
        '  call i64 @py_obj_contains(ptr %retained, ptr %key)')
    items = []

    def literal(match):
        index = next(count)
        items.append(f'@".static.items.{index}" = internal {holder} [1 x ptr] [ptr bitcast (ptr {match[1]} to ptr)]\n')
        return f'%items{index} = getelementptr inbounds [1 x ptr], ptr @".static.items.{index}", i32 0, i32 0'

    section = re.sub(r'call void @literal_use\(ptr (@"\.literal\.\d+")\)', literal, section)
    section = re.sub(r'\bcall (\w+) @', r'call \1 (ptr) @', section)
    return ''.join(items) + section


def _current_fixture(name, **shape):
    section, remaining = _fixture(name)
    return (_current_printer(section, **shape),
            {key: _current_printer(value, **shape) for key, value in remaining.items()})


@pytest.mark.parametrize('name', oracle.TAR_COMPILER_MODULES)
def test_strict_modules_accept_the_current_printer_shapes(name):
    section, remaining = _current_fixture(name)
    assert '@pcc_gc_retain(' in section or name in (oracle.CONSTANT_ONLY_MODULE, oracle._FREESTANDING)
    required = oracle.require_tar_attribute_module(name, _source(), section, remaining, require_definition)
    assert bool(required) == (name != oracle._FREESTANDING)


@pytest.mark.parametrize('name,damage', (
    (oracle._FREESTANDING, 'mutable_holder'),
    (oracle._UNSAFE, 'mutable_holder'),
    (oracle.CONSTANT_ONLY_MODULE, 'offset_alias'),
    (oracle.TAR_COMPILER_MODULES[5], 'offset_alias'),
    (oracle.TAR_COMPILER_MODULES[0], 'retained_other'),
    (oracle._WINDOWS, 'retained_other'),
))
def test_current_printer_shapes_still_reject_unbound_tables_and_literals(name, damage):
    shape = {'mutable_holder': {'holder': 'global'}, 'offset_alias': {'alias': 'offset'},
             'retained_other': {'retained': '%unrelated'}}[damage]
    section, remaining = _current_fixture(name, **shape)
    with pytest.raises(AssertionError):
        oracle.require_tar_attribute_module(name, _source(), section, remaining, require_definition)


def test_production_unsafe_registry_union_has_owned_strict_ir(tmp_path, monkeypatch, record_property):
    """Use the exact initializer and real exports, without the full closure."""
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    root = Path(__file__).resolve().parents[1]
    source = (root / 'pcc/frontends/python/codegen/unsafe_lowering.py').read_text()
    tree = ast.parse(source)
    policy = 'pcc.frontends.python.unsafe_intrinsic_policy'
    imported = next(node for node in tree.body
                    if isinstance(node, ast.ImportFrom) and node.module == policy)
    registry = next(node for node in tree.body
                    if isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == 'UNSAFE_INTRINSICS')
    # The production expression is deliberately extracted, not restated as a
    # smaller fixture that could drift away from the self-hosted initializer.
    probe = tmp_path / 'registry.py'
    probe.write_text(ast.get_source_segment(source, imported) + '\n'
                     + ast.get_source_segment(source, registry) + '\n')
    paths = [str(probe), str(root / (policy.replace('.', '/') + '.py'))]
    modules, exports, _ = build_closed_world_context(paths, [oracle._UNSAFE, policy])
    sections = {}
    for module in modules:
        typed = infer_module(module, external_exports=exports)
        codegen = L1CodeGen(typed, ir_scaffold_mode='on')
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        codegen._skip_program_main = True
        section = str(codegen.generate(typed))
        (tmp_path / (module.name + '.ll')).write_text(section)
        verify_ir_text(section)
        assert 'strict.nolib.stub' not in section
        assert not re.search(r'\b(?:call|invoke)\b[^\n]*@"?py_cpy_', section)
        sections[module.name] = section

    section = sections[oracle._UNSAFE]
    tables = (('UNSAFE_INTRINSICS', ('py_set_new', 'py_set_from_iterable')),)
    _, body = oracle._initializer(section, oracle._UNSAFE, tables, require_definition, None)
    flow = oracle._Flow(body)
    constructors = {result for result, callee, _args, _line in flow.calls
                    if result and callee in tables[0][1]}
    values = oracle._owned_set_union_results(flow)
    assert len(values) == 1
    # The new set is not either operand: this was the old oracle's false
    # failure. Identity-flow must not acquire a generic call-operand shortcut.
    slot = '@.modvar.' + oracle._UNSAFE.replace('.', '_') + '.UNSAFE_INTRINSICS'
    assert not flow.reaches(slot, constructors, rooted=True)
    assert flow.reaches(slot, values, rooted=True)
    with pytest.raises(AssertionError, match='unbound construction'):
        oracle._initializer(section.replace('@py_obj_or(', '@py_obj_xor('),
                            oracle._UNSAFE, tables, require_definition, None)
    routes = []
    for static in (True, False):
        directory = tmp_path / ("static-method-signature" if static else "dynamic-method-signature")
        directory.mkdir()
        routes.append(_check_real_three_argument_method_bindings_use_both_signature_representations(
            directory, monkeypatch, static,
        ))
    assert [route["static"] for route in routes] == [True, False]
    record_property("real_method_signature_ir", json.dumps(routes, sort_keys=True))


def _static_binding_fixture():
    pool = _Pool()
    section, body = _method_ir(pool)
    cells = []
    words = (4, 11, '__pcc_func_signature_v1__', 4, 7, 'self', 'intrinsic', 'expr',
             4, 7, 1, 1, 1, 4, 7, 12, 12, 12, 4, 7, 10, 10, 10)
    for word in words:
        if isinstance(word, int):
            cells.append(f'ptr inttoptr (i64 {word} to ptr)')
        else:
            name = pool.name(word)
            size = len(word.encode()) + 1
            data = ''.join('\\' + format(byte, '02X') for byte in word.encode()) + '\\00'
            obj = name.replace('.literal.', '.object.')
            pool.declarations += (
                f'{obj} = internal global {{ i64, i32, i32, i64, i64, i64, [{size} x i8] }} '
                f'{{ i64 1, i32 4, i32 1, i64 {size - 1}, i64 -1, i64 -1, [{size} x i8] c"{data}" }}\n'
            )
            cells.append(f'ptr bitcast (ptr {obj} to ptr)')
    descriptor = '@".signature.descriptor" = internal constant [23 x ptr] [' + ', '.join(cells) + ']\n'
    body = body.replace(
        '%signature_tuple = call ptr @py_tuple_new(i64 5)',
        '%descriptor = getelementptr inbounds [23 x ptr], ptr @".signature.descriptor", i32 0, i32 0\n'
        '  %signature_tuple = call ptr @py_static_aggregate_build(ptr %descriptor)',
    )
    body = re.sub(r'^  call void @py_tuple_set_item\(ptr %signature_tuple, i64 0,.*\n', '', body, flags=re.M)
    return pool.declarations + descriptor + section, body


def _check_static_unsafe_method_signatures_keep_real_class_binding():
    section, body = _static_binding_fixture()
    oracle.require_tar_unsafe_methods(_source(), section, body, require_definition)


def _static_constructor_binding_fixture():
    section, body = _static_binding_fixture()
    # The current runtime constructor owns these two allocations internally.
    body = re.sub(r'^  (?:%capture_tuple = |store ptr %capture_tuple, |%signature_tuple = |store ptr %signature_tuple, ).*\n',
                  '', body, flags=re.M)

    def cstring(name, value):
        data = ''.join('\\' + format(byte, '02X') for byte in value.encode()) + '\\00'
        return f'{name} = internal constant [{len(value.encode()) + 1} x i8] c"{data}"\n'

    section += cstring('@static_module', oracle._UNSAFE)
    calls = [call for call in oracle._Flow(body).calls if call[1] == 'py_func_new_signature_slots']
    assert len(calls) == 2
    for index, (_status, _callee, args, line) in enumerate(calls):
        method = (oracle.MUTATION_METHOD, oracle.DISPATCH_METHOD)[index][len(oracle._METHOD_PREFIX):]
        section += cstring(f'@static_qualname{index}', 'UnsafeIntrinsicMixin.' + method)
        replacement = (f'call i64 @py_func_new_static_def({args[2]}, {args[3]}, ptr %descriptor, '
                       f'ptr @static_module, ptr @static_qualname{index}, ptr null, i64 0, ptr null, {args[4]})')
        assert line in body
        body = body.replace(line, replacement, 1)
    return section, body


def _check_method_binding_preserves_exact_class_root_address_aliases(alias, *, constructor=False):
    section, body = _static_constructor_binding_fixture() if constructor else _static_binding_fixture()
    address = ('bitcast ptr %class_root to ptr' if alias == 'bitcast'
               else 'getelementptr [1 x ptr], ptr %class_root, i64 0, i64 0')
    body += (f'  %class_alias = {address}\n'
             '  store ptr %class, ptr %class_alias\n'
             '  %rooted_class = load ptr, ptr %class_root\n')
    body = body.replace('@py_class_add_method(ptr %class,', '@py_class_add_method(ptr %rooted_class,')
    body = body.replace('@py_class_setattr_raw(ptr %class,', '@py_class_setattr_raw(ptr %rooted_class,')
    oracle.require_tar_unsafe_methods(_source(), section, body, require_definition)


_STATIC_METHOD_BINDING_DAMAGES = (
    'mutable', 'short', 'long', 'tuple_kind', 'outer_count', 'inner_count', 'magic', 'name',
    'kind', 'flag', 'default', 'raw_string', 'object_tag', 'object_length', 'duplicate_descriptor',
    'offset', 'unknown_address', 'cyclic_address', 'loaded_address', 'build_arity', 'build_type',
    'build_argument_type', 'unused_build', 'wrong_class', 'both_wrong_class', 'unpublished_class',
    'class_offset', 'mixed_class_root', 'mixed_signature_root', 'mixed_callable_root',
    'class_constructor_type', 'class_constructor_arity', 'shared_frame_classes', 'object_class_name',
)


def _check_static_unsafe_method_binding_rejects_malformed_or_detached_values(damage, *, constructor=False):
    section, body = _static_constructor_binding_fixture() if constructor else _static_binding_fixture()
    original = (section, body)
    descriptor = next(line for line in section.splitlines() if line.startswith('@".signature.descriptor"'))
    replacements = {
        'mutable': ('internal constant [23 x ptr]', 'internal global [23 x ptr]'),
        'short': ('[23 x ptr]', '[22 x ptr]'),
        'long': ('[23 x ptr]', '[24 x ptr]'),
        'tuple_kind': ('ptr inttoptr (i64 4 to ptr)', 'ptr inttoptr (i64 6 to ptr)'),
        'outer_count': ('ptr inttoptr (i64 11 to ptr)', 'ptr inttoptr (i64 9 to ptr)'),
        'inner_count': ('ptr inttoptr (i64 7 to ptr)', 'ptr inttoptr (i64 5 to ptr)'),
        'kind': ('ptr inttoptr (i64 1 to ptr)', 'ptr inttoptr (i64 3 to ptr)'),
        'flag': ('ptr inttoptr (i64 12 to ptr)', 'ptr inttoptr (i64 14 to ptr)'),
        'default': ('ptr inttoptr (i64 10 to ptr)', 'ptr null'),
        'object_tag': ('i64 1, i32 4, i32 1', 'i64 1, i32 6, i32 1'),
        'object_length': ('i64 25, i64 -1', 'i64 24, i64 -1'),
    }
    if damage in replacements:
        old, new = replacements[damage]
        assert old in section
        section = section.replace(old, new, 1)
    elif damage in ('magic', 'name', 'raw_string'):
        objects = re.findall(r'ptr bitcast \(ptr (@"\.object\.\d+") to ptr\)', descriptor)
        target = objects[0 if damage != 'name' else 1]
        replacement = (objects[2] if damage == 'name' else objects[1]) if damage != 'raw_string' else target.replace('.object.', '.literal.')
        changed = section.replace('ptr ' + target + ' to ptr', 'ptr ' + replacement + ' to ptr', 1)
        assert changed != section
        section = changed
    elif damage == 'duplicate_descriptor':
        section += descriptor + '\n'
    elif damage in ('offset', 'unknown_address', 'cyclic_address', 'loaded_address'):
        old = '%descriptor = getelementptr inbounds [23 x ptr], ptr @".signature.descriptor", i32 0, i32 0'
        new = {
            'offset': old[:-1] + '1',
            'unknown_address': '%descriptor = inttoptr i64 16 to ptr',
            'cyclic_address': '%descriptor = bitcast ptr %cycle to ptr\n  %cycle = bitcast ptr %descriptor to ptr',
            'loaded_address': '%descriptor = load ptr, ptr @".signature.descriptor"',
        }[damage]
        body = body.replace(old, new)
    elif damage == 'build_arity':
        body = body.replace('@py_static_aggregate_build(ptr %descriptor)', '@py_static_aggregate_build(ptr %descriptor, ptr null)')
    elif damage == 'build_type':
        body = body.replace('call ptr @py_static_aggregate_build', 'call i64 @py_static_aggregate_build')
    elif damage == 'build_argument_type':
        body = body.replace('@py_static_aggregate_build(ptr %descriptor)', '@py_static_aggregate_build(i64 %descriptor)')
    elif damage == 'unused_build':
        body = body.replace('store ptr %signature_tuple,', 'store ptr %unrelated,')
    elif damage in ('wrong_class', 'both_wrong_class'):
        body = body.replace('@py_class_add_method(ptr %class,', '@py_class_add_method(ptr %other_class,')
        if damage == 'both_wrong_class':
            body = body.replace('@py_class_setattr_raw(ptr %class,', '@py_class_setattr_raw(ptr %other_class,')
    elif damage == 'unpublished_class':
        body = re.sub(r'^  store ptr %class, ptr @"\.class\.[^"]+"\n', '', body, flags=re.M)
    elif damage == 'class_offset':
        body += '  %offset_class = getelementptr i8, ptr %class, i64 8\n'
        body = body.replace('@py_class_add_method(ptr %class,', '@py_class_add_method(ptr %offset_class,')
        body = body.replace('@py_class_setattr_raw(ptr %class,', '@py_class_setattr_raw(ptr %offset_class,')
    elif damage == 'mixed_class_root':
        body += '  store ptr %class, ptr %mixed\n  store ptr %other_class, ptr %mixed\n  %mixed_class = load ptr, ptr %mixed\n'
        body = body.replace('@py_class_add_method(ptr %class,', '@py_class_add_method(ptr %mixed_class,')
        body = body.replace('@py_class_setattr_raw(ptr %class,', '@py_class_setattr_raw(ptr %mixed_class,')
    elif damage == 'mixed_signature_root':
        body += '  store ptr %unrelated, ptr %signature\n'
    elif damage == 'mixed_callable_root':
        body += '  store ptr %unrelated, ptr %output0\n'
    elif damage == 'class_constructor_type':
        body = body.replace('%class = call ptr @py_class_new', '%class = call i64 @py_class_new')
    elif damage == 'class_constructor_arity':
        body = body.replace(', ptr null, i32 0, ptr null, i32 0)', ', ptr null, i32 0)')
    elif damage == 'object_class_name':
        value = 'UnsafeIntrinsicMixin'
        data = ''.join('\\' + format(byte, '02X') for byte in value.encode()) + '\\00'
        section += ('@same_class_name = internal global { i64, i32, i32, i64, i64, i64, [21 x i8] } '
                    '{ i64 1, i32 4, i32 1, i64 20, i64 -1, i64 -1, [21 x i8] c"' + data + '" }\n')
        body = re.sub(r'@py_class_new\(ptr @"[^\"]+",', '@py_class_new(ptr @same_class_name,', body)
    else:
        # These two cells have the same frame base, but distinct object owners.
        body += ('  %first = getelementptr [2 x ptr], ptr %frame, i64 0, i64 0\n'
                 '  %second = getelementptr [2 x ptr], ptr %frame, i64 0, i64 1\n'
                 '  store ptr %class, ptr %first\n  store ptr %other_class, ptr %second\n'
                 '  %first_class = load ptr, ptr %first\n  %second_class = load ptr, ptr %second\n')
        body = body.replace('@py_class_add_method(ptr %class,', '@py_class_add_method(ptr %first_class,')
        body = body.replace('@py_class_setattr_raw(ptr %class,', '@py_class_setattr_raw(ptr %second_class,')
    assert (section, body) != original, (damage, "ineffective mutation")
    try:
        oracle.require_tar_unsafe_methods(_source(), section, body, require_definition)
    except AssertionError:
        return
    pytest.fail("static method binding accepted damage: " + damage)


_STATIC_CONSTRUCTOR_SHARED_DAMAGES = tuple(
    damage for damage in _STATIC_METHOD_BINDING_DAMAGES
    if damage not in ('build_arity', 'build_type', 'build_argument_type', 'unused_build', 'mixed_signature_root')
)
_STATIC_CONSTRUCTOR_DAMAGES = (
    'arity', 'return_type', *(f'argument_type_{index}' for index in range(9)),
    'entry', 'name', 'descriptor', 'module', 'qualname', 'doc', 'role',
    'auto_park_role', 'continuation_role', 'cache',
    'null_output', 'offset_output', 'detached_output', 'status_value', 'dormant_constructor',
)


def _check_static_constructor_binding_rejects_damage(damage):
    section, body = _static_constructor_binding_fixture()
    original = body
    _status, _callee, args, line = next(call for call in oracle._Flow(body).calls
                                      if call[1] == 'py_func_new_static_def')
    damaged = list(args)
    if damage == 'arity':
        damaged.append('ptr null')
    elif damage.startswith('argument_type_'):
        index = int(damage.rsplit('_', 1)[1])
        damaged[index] = ('ptr ' if index == 6 else 'i64 ') + args[index].split(' ', 1)[1]
    elif damage == 'return_type':
        body = body.replace(line, line.replace('call i64 ', 'call ptr '), 1)
    elif damage == 'status_value':
        body = body.replace('store ptr %callable0,', 'store ptr %status0,', 1)
    elif damage == 'dormant_constructor':
        body = body.replace(line, line.replace('@py_func_new_static_def(', '@unrelated('), 1)
        body += '  %unused_status = ' + line.replace(args[8], 'ptr %unused_output') + '\n'
    else:
        index, value = {
            'entry': (0, 'ptr @wrong_adapter'), 'name': (1, 'ptr @static_module'),
            'descriptor': (2, 'ptr %unrelated'), 'module': (3, args[4]),
            'qualname': (4, args[3]), 'doc': (5, 'ptr %unrelated'),
            'role': (6, 'i64 1'), 'auto_park_role': (6, 'i64 8388608'),
            'continuation_role': (6, 'i64 67108864'), 'cache': (7, 'ptr %publication0'),
            'null_output': (8, 'ptr null'),
            'offset_output': (8, 'ptr getelementptr ([2 x ptr], ptr %output0, i64 0, i64 1)'),
            'detached_output': (8, 'ptr %unrelated'),
        }[damage]
        damaged[index] = value
    if damaged != args:
        body = body.replace(line, 'call i64 @py_func_new_static_def(' + ', '.join(damaged) + ')', 1)
    assert body != original, (damage, 'ineffective mutation')
    with pytest.raises(AssertionError, match='not bound to real class'):
        oracle.require_tar_unsafe_methods(_source(), section, body, require_definition)


def _check_real_three_argument_method_bindings_use_both_signature_representations(tmp_path, monkeypatch, static):
    """Actual one-module host IR, without compiling the full PCC closure."""
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv('PCC_STATIC_AGGREGATE', '1' if static else '0')
    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    monkeypatch.setenv('PCC_PYTHON_IR_PASS_JOBS', '1')
    for name in ('PCC_DIRECT_INDEXED_KERNEL_CAPTURE', 'PCC_DIRECT_INDEXED_KERNEL_EMIT', 'PCC_HOST_INDEXED_PROCESS_SPLIT'):
        monkeypatch.setenv(name, '0')
    monkeypatch.delenv('PCC_PYTHON_IR_PASSES', raising=False)
    monkeypatch.delenv('PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES', raising=False)
    probe = tmp_path / 'methods.py'
    probe.write_text('UNSAFE_INTRINSICS = None\n'
                     'class UnsafeIntrinsicMixin:\n'
                     '    _UNSAFE_INTRINSICS = UNSAFE_INTRINSICS\n'
                     '    def _emit_unsafe_file_mutation(self, intrinsic, expr):\n'
                     '        return expr\n'
                     '    def _emit_unsafe_intrinsic_call(self, intrinsic, expr):\n'
                     '        return expr\n')
    output = tmp_path / 'methods.ll'
    compile_python_multi([str(probe)], str(output), module_names=[oracle._UNSAFE],
                         entry_module=oracle._UNSAFE, emit_llvm_only=True, target_triple=host_target_triple(),
                         libpython_mode='off', ir_scaffold_mode='on', backend='self', recursive_stdlib=False)
    assert 0 < output.stat().st_size < 2 * 1024 * 1024
    section = output.read_text()
    verify_ir_text(section)
    assert 'strict.nolib.stub' not in section and not re.search(r'\b(?:call|invoke)\b[^\n]*@"?py_cpy_', section)
    # A single entry module has the same class-body lowering in its public
    # module-init function; closure dependencies call it module-top instead.
    initializer = '_pcc_py_module_init_' + oracle._UNSAFE.replace('.', '_')
    body = oracle._definition(section, initializer).group('body')
    assert bool(re.search(r'@"?\.pcc\.static\.agg\.func\.sig\.\d+"? = internal constant \[23 x ptr\]', section)) == static
    flow = oracle._Flow(body)
    constructors = [callee for _, callee, _, _ in flow.calls
                    if callee in ('py_func_new_static_def', 'py_func_new_signature_slots')]
    constructor = 'py_func_new_static_def' if static else 'py_func_new_signature_slots'
    assert constructors == [constructor, constructor]
    # The static constructor now builds captures/signature under runtime roots;
    # the original inline static-builder path remains covered above.
    assert not any(callee == 'py_static_aggregate_build' for _, callee, _, _ in flow.calls)
    assert any(callee == 'py_tuple_new' and args == ['i64 5'] for _, callee, args, _ in flow.calls) != static
    helper = getattr(oracle, '_require_method_class_binding', None)
    if helper is None:
        # Running this same probe against the exact preimage must fail at the
        # old class-binding assertion, before the deliberately small bodies'
        # unrelated production diagnostic checks.
        oracle.require_tar_unsafe_methods(_source(), section, body, require_definition)
    else:
        for symbol in (oracle.MUTATION_METHOD, oracle.DISPATCH_METHOD):
            helper(section, body, symbol)
    return {"static": static, "constructor": constructor, "ir_bytes": output.stat().st_size,
            "ir_sha256": hashlib.sha256(section.encode()).hexdigest(), "native_execution": False}
