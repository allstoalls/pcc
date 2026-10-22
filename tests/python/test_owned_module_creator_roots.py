"""Check emitted runtime owner transfers, independently of collector models."""
from pathlib import Path
import re

import pytest

from tests.python.test_owned_import_runtime_roots import _assert_new_is_published, _ir_body

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('threaded', ['0', '1'])
@pytest.mark.parametrize('module', ['py_module_attrs_runtime', 'py_compiled_module_runtime',
                                    'py_extension_loader_runtime'])
def test_module_creators_publish_new_results_before_parking(tmp_path, monkeypatch, module, threaded):
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv('PCC_WITH_THREADS', threaded)
    output = tmp_path / (module + '.ll')
    _compile_runtime_module(module, str(ROOT / 'pcc/runtime/py' / (module + '.py')),
                            str(output), 'x86_64-unknown-linux-gnu')
    text = output.read_text()
    verify_ir_text(text)
    assert 'strict.nolib.stub:' not in text
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    if module == 'py_module_attrs_runtime':
        body = _ir_body(text, 'py_sys_modules_find')
        _assert_new_is_published(body, 'py_dict_get')
        finish = _ir_body(text, 'py_sys_modules_owner_finish')
        transfer = finish.index('@pcc_gc_take_pinned_slot(')
        assert finish.rfind('@pcc_gc_frame_leave(') < transfer
        assert not re.search(r'\bcall\b', finish[finish.index('\n', transfer):])
        return
    if module == 'py_compiled_module_runtime':
        factory = _ir_body(text, 'user_' + module + '__create_compiled_module_node')
        _assert_new_is_published(factory, 'py_instance_new')
        _assert_new_is_published(factory, 'py_str_new')
        body = _ir_body(text, 'user_' + module + '__compiled_module_import_into')
        wrapper_names = ['py_compiled_module_import_by_name']
        mapping = 'pcc_compiled_module_frame_map'
    else:
        body = _ir_body(text, 'user_' + module + '__native_extension_import_into')
        _assert_new_is_published(body, 'pcc_capi_module_from_def')
        assert '@pcc_capi_moduledef_marker' in body
        assert '@pcc_capi_is_moduledef' not in body
        # The union classifier is inline, so a raw definition is never leased
        # merely to survive a polling classification helper.
        wrapper_names = ['py_native_extension_import', 'py_native_extension_import_by_name']
        mapping = 'pcc_extension_module_frame_map'
    _assert_new_is_published(body, 'py_sys_modules_import_cached')
    _assert_new_is_published(body, 'py_sys_modules_finish_import')
    for name in wrapper_names:
        wrapper = _ir_body(text, name)
        # The frame map is passed directly, or through an identity alias.
        frame = re.search(r'call void \(ptr, ptr\) @pcc_gc_frame_enter\(ptr ([%@][-\w.]+), ptr ', wrapper)
        assert frame and (frame[1] == '@' + mapping or re.search(
            re.escape(frame[1]) + r' = bitcast ptr @' + mapping + r' to ptr', wrapper))
        terminal = wrapper.index('@py_sys_modules_owner_finish(')
        assert re.match(r'\s*ret ptr ', wrapper[wrapper.index('\n', terminal):])
