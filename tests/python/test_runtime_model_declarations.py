"""The model loader reads the same named layouts as runtime declarations."""
import ast
import pytest
from tests.python.runtime_model_declarations import global_i32_declarations


def read(source, bindings=None):
    return global_i32_declarations(ast.parse(source), bindings)


def test_named_return_map_and_negative_borrowed_count():
    assert read('COUNT = 2\nBORROWED = -3\ndefine_global_i32("result", COUNT)\ndefine_global_i32("borrowed", BORROWED)') == {'result':2,'borrowed':-3}


def test_source_order_aliases_annotations_and_imported_abi_constants():
    assert read('COUNT: int = POINTER_SIZE // 4\nALIAS = COUNT\ndefine_global_i32("first", ALIAS)\nCOUNT = 3\ndefine_global_i32("second", COUNT)', {'POINTER_SIZE':8}) == {'first':2,'second':3}


def test_count_names_inside_functions_do_not_rebind_module_constants():
    assert read('COUNT = 2\ndef f():\n    COUNT = 99\ndefine_global_i32("count", COUNT)') == {'count':2}


@pytest.mark.parametrize('source', (
    'define_global_i32("map", UNKNOWN)',
    'COUNT: int\ndefine_global_i32("map", COUNT)',
    'COUNT = missing_call()\ndefine_global_i32("map", COUNT)',
    'COUNT = 2\nCOUNT = missing_call()\ndefine_global_i32("map", COUNT)',
    'COUNT = 2\ndef COUNT():\n    pass\ndefine_global_i32("map", COUNT)',
    'define_global_i32("map", 1 // 0)',
    'define_global_i32("map", 1 << -1)',
    'define_global_i32("map", 1 << 64)',
    'define_global_i32("map", True)',
    'define_global_i32("map", "2")',
    'define_global_i32("map", 1)\ndefine_global_i32("map", 2)',
))
def test_unknown_dynamic_or_malformed_counts_fail_closed(source):
    with pytest.raises(ValueError):
        read(source)


@pytest.mark.parametrize('statement', (
    'COUNT += 1',
    'del COUNT',
    'COUNT, OTHER = (3, 4)',
    'OTHER = (COUNT := 3)',
    'if flag:\n    COUNT = 3',
    'if False:\n    COUNT = 3',
    'if flag:\n    pass\nelse:\n    COUNT = 3',
    'for COUNT in values:\n    pass',
    'for value in values:\n    COUNT = 3',
    'for value in values:\n    pass\nelse:\n    COUNT = 3',
    'while flag:\n    COUNT = 3',
    'try:\n    COUNT = 3\nfinally:\n    pass',
    'try:\n    pass\nexcept Exception as COUNT:\n    pass',
    'try:\n    pass\nfinally:\n    del COUNT',
    'with resource() as COUNT:\n    pass',
    'match value:\n    case COUNT:\n        pass',
    'match value:\n    case [*COUNT]:\n        pass',
    'match value:\n    case {**COUNT}:\n        pass',
    'if flag:\n    def COUNT():\n        pass',
    'if flag:\n    class COUNT:\n        pass',
    'if flag:\n    import provider as COUNT',
    'if flag:\n    from provider import COUNT',
))
def test_unsupported_module_rebindings_invalidate_stale_constants(statement):
    with pytest.raises(ValueError, match='unresolved runtime declaration constant: COUNT'):
        read('COUNT = 2\n' + statement + '\ndefine_global_i32("count", COUNT)')


@pytest.mark.parametrize('statement', (
    'def f():\n    COUNT += 1\n    del COUNT',
    'async def f():\n    COUNT = 99',
    'class C:\n    COUNT = 99',
    'if flag:\n    def f():\n        COUNT = 99',
    'for value in values:\n    class C:\n        COUNT = 99',
    'callback = lambda: (COUNT := 99)',
))
def test_nested_scope_bindings_do_not_invalidate_module_constants(statement):
    assert read('COUNT = 2\n' + statement + '\ndefine_global_i32("count", COUNT)') == {'count': 2}


def test_unrelated_conditional_binding_does_not_invalidate_module_constant():
    assert read('COUNT = 2\nif flag:\n    OTHER = 3\ndefine_global_i32("count", COUNT)') == {'count': 2}


def test_explicit_static_assignment_recovers_an_invalidated_constant():
    assert read('COUNT = 2\nCOUNT += 1\nCOUNT = 4\ndefine_global_i32("count", COUNT)') == {'count': 4}
