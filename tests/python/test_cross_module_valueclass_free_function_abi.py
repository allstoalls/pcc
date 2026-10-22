from __future__ import annotations

import re

import pytest


@pytest.fixture
def valueclass_function_modules(tmp_path):
    sources = {
        "records": """
def valueclass(cls):
    return cls

@valueclass
class Pair:
    first: int
    second: int

    def first_value(self) -> int:
        return self.first
""",
        "aliases": "from records import Pair as Payload\n",
        "provider": """
from aliases import Payload as Handle

def relay(handle: Handle, count: int = 1, *, flag: bool = False) -> int:
    return handle.first + handle.second + count
""",
        "consumer": """
from records import Pair
from provider import relay as forwarded

def positional(handle: Pair) -> int:
    return forwarded(handle, 2)

def keyword(handle: Pair) -> int:
    return forwarded(flag=True, handle=handle)
""",
    }
    paths = []
    for name, source in sources.items():
        path = tmp_path / (name + ".py")
        path.write_text(source.lstrip(), encoding="utf-8")
        paths.append(str(path))
    return paths, list(sources)


def test_valueclass_function_signature_expansion_preserves_defaults_and_wire(
    tmp_path, valueclass_function_modules,
):
    from pcc.frontends.python import pipeline_exports as exports_api
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.py_ast import FuncDef

    paths, names = valueclass_function_modules
    parsed, exports, derived = build_closed_world_context(paths, names)
    relay = exports["provider"]["relay"]
    handle_arg = next(arg for arg in relay["call_sig"] if arg["name"] == "handle")
    assert handle_arg["annotation"] == relay["param_types"][0]
    assert handle_arg["annotation"][:3] == ("valueclass", "Pair", "records")
    provider_ast = parsed[names.index("provider")]
    function = next(stmt for stmt in provider_ast.body if isinstance(stmt, FuncDef))
    raw_signature = exports_api._export_call_sig(function.args, "provider", ("relay",))
    for raw, expanded in zip(raw_signature, relay["call_sig"], strict=True):
        assert {key: value for key, value in expanded.items() if key != "annotation"} == {
            key: value for key, value in raw.items() if key != "annotation"
        }

    before = exports_api._native_export_to_wire(exports)
    for _repeat in range(2):
        for name in names:
            exports_api._expand_local_valueclass_export_refs(name, exports[name])
        assert exports_api._native_export_to_wire(exports) == before
    receiver = next(method for method in exports["records"]["Pair"]["methods"]
                    if method["name"] == "first_value")
    assert receiver["call_sig"][0]["annotation"] == receiver["param_types"][0]
    assert receiver["call_sig"][0]["annotation"][0] == "valueclass"

    path = tmp_path / "native-exports.json"
    exports_api._write_native_exports_wire(path, exports, derived, ())
    restored, restored_derived = exports_api._read_native_exports_wire(path)
    assert restored_derived == derived
    assert exports_api._native_export_to_wire(restored) == before
    assert [arg["annotation"] for arg in restored["provider"]["relay"]["call_sig"]] == [
        arg["annotation"] for arg in relay["call_sig"]
    ]


@pytest.mark.parametrize("signature_state", ("absent", "none"))
def test_valueclass_function_expansion_keeps_optional_signature_metadata(
    valueclass_function_modules, signature_state,
):
    from pcc.frontends.python.codegen.extern_func_info_lowering import ExternFuncInfoLoweringMixin
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.pipeline_exports import _expand_local_valueclass_export_refs

    paths, names = valueclass_function_modules
    _, exports, _ = build_closed_world_context(paths, names)
    relay = exports["provider"]["relay"]
    if signature_state == "absent":
        relay.pop("call_sig")
    else:
        relay["call_sig"] = None
    _expand_local_valueclass_export_refs("provider", exports["provider"])
    assert relay.get("call_sig") is None
    assert ("call_sig" in relay) == (signature_state == "none")
    assert ExternFuncInfoLoweringMixin._extern_info_to_funcdef(None, "relay", relay) is None
    assert relay["param_types"][0][:3] == ("valueclass", "Pair", "records")


def test_cross_module_valueclass_function_positional_and_keyword_calls_use_payload_abi(
    tmp_path, valueclass_function_modules,
):
    from pcc.diagnostics.ir_diff import IrSummary
    from pcc.frontends.python.pipeline_context import compile_contextual_per_module_fallback_counts

    paths, names = valueclass_function_modules
    counts = compile_contextual_per_module_fallback_counts(
        paths, names, names, ir_scaffold_mode="on", strict_no_libpython=True,
        emit_ir_dir=str(tmp_path),
    )
    assert counts == {name: 0 for name in names}
    provider_ir = (tmp_path / "provider.ll").read_text(encoding="utf-8")
    assert re.search(r"define external (?:ptr|i64) @user_provider_relay\(\{ i64, i64 \} %handle", provider_ir)
    consumer_ir = (tmp_path / "consumer.ll").read_text(encoding="utf-8")
    functions = IrSummary.parse(consumer_ir).functions
    for name in ("positional", "keyword"):
        function = functions["user_consumer_" + name]
        assert "user_provider_relay" in function.calls
        assert "py_obj_call" not in function.calls
        assert "py_valuebox_new" not in function.calls
        assert "py_valuebox_get_field" not in function.calls
    assert len(re.findall(r"call (?:ptr|i64) \(\{ i64, i64 \},[^\n]+@user_provider_relay\(\{ i64, i64 \}", consumer_ir)) == 2
    assert "strict.nolib.stub" not in consumer_ir
