"""Import statements publish the binding used by managed provider calls.

These bounded frontend checks deliberately retain default-bearing provider
signatures: a direct fixed-arity call concealed the missing import binding.
Native integration remains owned by the unchanged walk and writer fixtures.
"""

import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module


PROVIDERS = {
    "os": "def walk(root, topdown=True, onerror=None, followlinks=False):\n    return root\n",
    "fcntl": "F_GETFL = 3\ndef fcntl(fd, cmd, arg=0):\n    return fd\n",
    "provider": "value = object()\ndef fetch(value=None):\n    return value\n",
}


def emit(tmp_path, monkeypatch, source, provider="provider"):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider_path = tmp_path / (provider + ".py")
    consumer_path = tmp_path / "consumer.py"
    provider_path.write_text(PROVIDERS[provider])
    consumer_path.write_text(source)
    modules, exports, derived = build_closed_world_context(
        [str(provider_path), str(consumer_path)], [provider, "consumer"],
    )
    external = {provider: exports[provider]}
    typed = infer_module(modules[-1], external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._native_module_exports = external
    codegen._sibling_module_inits = (provider,)
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(typed))
    (tmp_path / "consumer.ll").write_text(text)
    has_unavailable_function = "strict.nolib.stub:" in text
    assert not has_unavailable_function, "import binding emitted an unavailable function"
    return text


def body(text, name):
    match = re.search(r"^define [^\n]*@" + re.escape(name) + r"\([^\n]*\).*?^}", text, re.M | re.S)
    assert match, name
    return match.group(0)


@pytest.mark.parametrize("function", [
    "def probe(root):\n    return os.walk(root)\n",
    "def probe(root):\n    for item in os.walk(root):\n        yield item\n",
])
def test_builtin_compiled_provider_import_publishes_real_binding(tmp_path, monkeypatch, function):
    text = emit(tmp_path, monkeypatch, "import os\n" + function, "os")
    top = body(text, "main")
    assert bool(re.search(r"call [^\n]*@py_module_attr_set[^\n]*@\.pyattr\.os[, ]", top)), "executed import did not publish os"
    assert "@.pcc.ext.modref.os" not in text


@pytest.mark.parametrize("prefix", ["", "    global fcntl\n"])
def test_local_or_explicit_global_import_matches_binding_scope(tmp_path, monkeypatch, prefix):
    text = emit(tmp_path, monkeypatch, "def probe(descriptor):\n" + prefix +
                "    import fcntl\n    try:\n        return fcntl.fcntl(descriptor, fcntl.F_GETFL)\n"
                "    except ValueError:\n        return None\n", "fcntl")
    probe = body(text, "user_consumer_probe")
    assert "@.pcc.ext.modref.fcntl" not in text
    if prefix:
        assert "@.modvar.consumer.fcntl" in probe
        assert "name.dynamic.fcntl" in probe
    else:
        assert "name.dynamic.fcntl" not in probe
        assert bool(re.search(r"%fcntl(?:\.[\w.]+)?\.addr[^\n]*= alloca ptr", probe)), "local module lacks an owning slot"
        assert "@pcc_gc_root_move" in probe


@pytest.mark.parametrize("source", [
    "from provider import value\ndef probe():\n    return value\n",
    "def probe():\n    from provider import value\n    return value\n",
    "from provider import *\ndef probe():\n    return value\n",
])
def test_compiled_import_producers_publish_before_error_checks(tmp_path, monkeypatch, source):
    text = emit(tmp_path, monkeypatch, source)
    producers = ("py_compiled_module_import_by_name",) if "import *" in source else (
        "py_compiled_module_import_by_name", "py_obj_getattr",
    )
    for producer in producers:
        calls = re.findall(r"^\s*(%[\w.]+) = call [^\n]*@" + producer + r"\([^\n]*\)\n([^\n]*)", text, re.M)
        assert calls, producer
        for value, next_line in calls:
            assert re.search(r"store ptr " + re.escape(value) + r", ptr %", next_line), (producer, next_line)
    assert "@.pcc.ext.modref." not in text


def test_sys_modules_import_uses_slot_binding(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch, "def probe():\n    from sys import modules\n    return modules\n")
    probe = body(text, "user_consumer_probe")
    assert "@pcc_gc_root_move" in probe
    assert "@pcc_gc_pin(" not in probe
    assert "@pcc_gc_unpin(" not in probe


@pytest.mark.parametrize("source", [
    "import extension\ndef probe():\n    return extension\n",
    "def probe():\n    import extension\n    return extension\n",
    "from extension import value\ndef probe():\n    return value\n",
    "def probe():\n    from extension import value\n    return value\n",
    "def bind():\n    global value\n    from extension import value\ndef probe():\n    return value\n",
    "from extension import *\ndef probe():\n    return dynamic_export\n",
])
def test_native_extension_import_producers_share_slot_contract(tmp_path, monkeypatch, source):
    monkeypatch.setattr(
        L1CodeGen, "_resolve_pcc_native_extension_path",
        lambda self, name: "/diagnostic/extension.so" if name == "extension" else None,
    )
    text = emit(tmp_path, monkeypatch, source)
    producers = ["py_native_extension_import"]
    if "from extension import value" in source:
        producers.append("py_obj_getattr")
    for producer in producers:
        calls = re.findall(r"^\s*(%[\w.]+) = call [^\n]*@" + producer + r"\([^\n]*\)\n([^\n]*)", text, re.M)
        assert calls, producer
        for value, next_line in calls:
            assert bool(re.search(r"store ptr " + re.escape(value) + r", ptr %", next_line)), (producer, next_line)
    assert "@.pcc.ext.modref." not in text
    if "import *" in source:
        assert bool(re.search(r"call [^\n]*@py_module_import_star", text))
        assert "@.modvar.consumer.starimport" not in text


def test_guarded_module_import_has_a_checked_global_binding(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch,
                "try:\n    import fcntl\nexcept ImportError:\n    pass\n"
                "def probe(descriptor):\n    return fcntl.fcntl(descriptor, fcntl.F_GETFL)\n", "fcntl")
    probe = body(text, "user_consumer_probe")
    assert "name.dynamic.fcntl.missing" in probe
    assert "@py_module_attr_get" in probe


def test_from_import_global_is_unbound_until_the_statement_runs(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch,
                "def probe():\n    return value\nprobe()\nfrom provider import value\n")
    probe = body(text, "user_consumer_probe")
    assert "name.dynamic.value.missing" in probe
    assert "@py_module_attr_get" in probe


def test_synthetic_default_provider_retains_original_consumer_signatures():
    from pathlib import Path
    import ast
    root = Path(__file__).resolve().parents[2]
    for path, function, count in [
        (root / "pcc/stdlib/os.py", "walk", 3),
        (root / "pcc/stdlib/fcntl.py", "fcntl", 1),
    ]:
        node = next(item for item in ast.parse(path.read_text()).body
                    if isinstance(item, ast.FunctionDef) and item.name == function)
        assert len(node.args.defaults) == count
