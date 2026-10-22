"""Compile the real handoff provider, verify its closure, and emit AArch64.

This is a host-pcc0 compile-only gate. It builds no runtime, links no program,
executes no emitted code, and makes no Stage1 or native-behavior claim.
"""

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROVIDER = "pcc.frontends.python.pipeline_indexed_handoff"
SOURCE = ROOT / "pcc/frontends/python/pipeline_indexed_handoff.py"
EXPECTED_CLOSURE_MODULES = {PROVIDER, "hashlib", "json", "os", "warnings"}
SECTION = re.compile(r"^; ---- module: ([A-Za-z_][\w.]*) ----$", re.M)
DEFINITION = re.compile(r'^define[^\n]*@"?([A-Za-z_][\w.$]*)"?\(', re.M)
CPY_CALL = re.compile(r'\b(?:call|invoke)\b[^\n]*@"?py_cpy_')
NOT_IMPLEMENTED = re.compile(r'@"?py_exc_new"?\(\s*i64\s+11\s*,')


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _function(text, symbol):
    # A header must name THIS function. Never consume a later definition when
    # the requested function was reduced to a declaration or omitted entirely.
    pattern = (r'^define[^\n]*@"?' + re.escape(symbol)
               + r'"?\([^\n]*\)[^\n]*\{\n(?P<body>.*?)^\}')
    matches = list(re.finditer(pattern, text, re.M | re.S))
    assert len(matches) == 1, (symbol, "missing or duplicate definition")
    match = matches[0]
    body = match.group("body")
    assert not re.search(r"^define\b", body, re.M), (symbol, "unterminated definition")
    assert "strict.nolib" not in body, (symbol, "strict no-libpython stub")
    assert "@py_cpy_" not in body and '@"py_cpy_' not in body, symbol
    assert "NotImplementedError" not in body and not NOT_IMPLEMENTED.search(body), (
        symbol, "NotImplementedError fallback")
    assert re.search(r"\bcall\b", body), (symbol, "empty/return-only helper")
    return match


def _check_body_guard(provider_text, symbol):
    """Negative controls mutate an actual emitted helper, not a fake export."""
    original = _function(provider_text, symbol)
    header = original.group(0).split("\n", 1)[0]
    replacements = (
        "",  # Must not accidentally match the following helper.
        "declare ptr @" + symbol + "()\n",
        header + "\nentry:\n  ret ptr null\n}",
        header + "\nstrict.nolib.stub:\n  call void @py_raise(ptr null)\n  ret ptr null\n}",
        header + "\nentry:\n  call ptr @py_cpy_import(ptr null)\n  ret ptr null\n}",
        # The native fallback stores exception kind 11, not necessarily the
        # human-readable exception name inside the function's IR body.
        header + "\nentry:\n  call ptr @py_exc_new(i64 11, ptr null)\n  ret ptr null\n}",
    )
    for replacement in replacements:
        mutated = (provider_text[:original.start()] + replacement
                   + provider_text[original.end():])
        with pytest.raises(AssertionError):
            _function(mutated, symbol)
    return len(replacements)


def test_handoff_owned_import_census_source_policy():
    """Source-only census catches imports inside every owned provider body."""
    expected_imports = {
        PROVIDER: {"hashlib", "json", "os"},
        "hashlib": {"__future__", "pcc.extern", "pcc.unsafe"},
        "json": {"__future__"},
        "os": {"__future__", "os", "sys", "pcc.extern", "pcc.unsafe", "warnings"},
        "warnings": {"__future__", "re", "sys"},
    }
    assert set(expected_imports) == EXPECTED_CLOSURE_MODULES
    trees = {}
    for name, expected in expected_imports.items():
        path = SOURCE if name == PROVIDER else ROOT / "pcc/stdlib" / (name + ".py")
        tree = ast.parse(path.read_bytes(), filename=str(path))
        trees[name] = tree
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (name, "review new relative provider import")
                imports.add(node.module)
        assert imports == expected, (name, "review changed owned imports", imports)
    fsync = next(node for node in trees["os"].body
                 if isinstance(node, ast.FunctionDef) and node.name == "fsync")
    assert any(isinstance(node, ast.Import) and any(alias.name == "warnings" for alias in node.names)
               for node in ast.walk(fsync)), "owned os.fsync must retain its warnings provider edge"
    # Read literal policy tables without importing PCC or invoking discovery.
    path = ROOT / "pcc/frontends/python/pipeline_import_policy.py"
    tree = ast.parse(path.read_bytes(), filename=str(path))
    tables = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {
                "NATIVE_BUILTIN_IMPORTS", "REQUIRED_COMPILED_STDLIB_PROVIDERS",
            }:
                value = node.value
                assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                assert value.func.id == "frozenset" and len(value.args) == 1 and not value.keywords
                tables[target.id] = ast.literal_eval(value.args[0])
    native = tables["NATIVE_BUILTIN_IMPORTS"]
    required = tables["REQUIRED_COMPILED_STDLIB_PROVIDERS"]
    assert {"os", "json"} <= required
    assert {"hashlib", "warnings"}.isdisjoint(native)
    assert {"re", "sys"} <= native and {"re", "sys"}.isdisjoint(required)


def test_handoff_provider_closed_world_aarch64(tmp_path, monkeypatch):
    assert sys.platform == "darwin" and platform.machine() == "arm64", (
        "this independent gate requires the Darwin arm64 CI runner")
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_module
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from pcc.frontends.python import pipeline_import_policy as policy
    from pcc.tools.runtime_archive_provenance import codegen_checksum
    from tests.owned_ir_validation import verify_ir_text

    source = SOURCE.read_bytes()
    tree = ast.parse(source, filename=str(SOURCE))
    declared = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert declared and len(declared) == len(set(declared))
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in tree.body)
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    assert imports == {"hashlib", "json", "os"}, "review the changed import closure"
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree))
    assert {"json", "os"} <= policy.REQUIRED_COMPILED_STDLIB_PROVIDERS
    assert {"json", "os"} <= policy.NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER
    assert "hashlib" not in policy.NATIVE_BUILTIN_IMPORTS
    sources = {PROVIDER: SOURCE, **{name: ROOT / "pcc/stdlib" / (name + ".py")
                                  for name in sorted(imports)}}
    # The owned-provider queue also scans function bodies: os.fsync imports warnings.
    sources["warnings"] = ROOT / "pcc/stdlib/warnings.py"
    assert set(sources) == EXPECTED_CLOSURE_MODULES
    source_hashes = {name: _sha256(path.read_bytes()) for name, path in sources.items()}
    target = host_target_triple()
    assert target.startswith(("arm64-apple-darwin", "aarch64-apple-darwin")), target
    options = dict(emit_llvm_only=True, target_triple=target, libpython_mode="off",
                   ir_scaffold_mode="on", backend="self", recursive_stdlib=False)
    # One in-process frontend path, with owned default passes. These are env
    # settings only: no fake exports, private reassembly, or compiler patching.
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASS_JOBS", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.delenv("PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES", raising=False)
    receipt = {
        "schema": "pcc.indexed-handoff-closed-world.v1", "status": "RUNNING",
        "scope": "host pcc0 public closed-world IR; owned whole-output verification; "
                 "real helper indexed AArch64 assembly emission only",
        "native_execution": False, "runtime_built": False,
        "provider": PROVIDER, "options": options,
        "source_sha256": source_hashes, "test_sha256": _sha256(Path(__file__).read_bytes()),
        "declared_functions": declared, "verified_functions": [],
        "codegen_sha256": codegen_checksum(), "phase": "compile",
    }
    output = tmp_path / "indexed-handoff-closure.ll"
    try:
        assert re.fullmatch(r"[0-9a-f]{64}", receipt["codegen_sha256"]), "unknown codegen identity"
        # ONLY the real helper is supplied. Public import policy must admit
        # all owned providers itself; nothing is reconstructed by the test.
        compile_python_multi([str(SOURCE)], str(output), module_names=[PROVIDER],
                             entry_module=PROVIDER, **options)
        text = output.read_text(encoding="utf-8")
        receipt["ir_sha256"] = _sha256(output.read_bytes())
        receipt["phase"] = "closure-and-bodies"
        markers = list(SECTION.finditer(text))
        assert markers and not text[:markers[0].start()].strip()
        names = [match.group(1) for match in markers]
        assert len(names) == len(set(names)), "duplicate provider sections"
        assert set(names) == set(sources), ("unexpected/missing owned provider", names)
        sections = {match.group(1): text[match.end():markers[index + 1].start()
                    if index + 1 < len(markers) else len(text)]
                    for index, match in enumerate(markers)}
        receipt["closure_modules"] = names
        # Reject native fallback stubs and executed CPython dependencies in
        # the entire closure. Legitimate provider exception branches remain
        # legal; NotImplementedError rejection is specific to helper bodies.
        assert "strict.nolib.stub" not in text, "closure contains native fallback stub"
        assert not CPY_CALL.search(text), "closure contains a CPython fallback call"
        provider_text = sections[PROVIDER]
        for name in declared:
            symbol = "user_" + PROVIDER.replace(".", "_") + "_" + name
            _function(provider_text, symbol)
            receipt["verified_functions"].append(name)
        receipt["body_guard_negative_controls"] = _check_body_guard(
            provider_text, "user_" + PROVIDER.replace(".", "_") + "_" + declared[0])
        receipt["phase"] = "owned-whole-output-verification"
        whole = verify_ir_text(text)
        assert Counter(function.name for function in whole.functions) == Counter(
            DEFINITION.findall(text)), "owned parser omitted or duplicated a definition"
        receipt["verified_closure_definition_count"] = len(whole.functions)
        del whole
        receipt["phase"] = "indexed-aarch64-emission"
        provider_module = verify_ir_text(provider_text)
        assert provider_module.triple == target
        assert Counter(function.name for function in provider_module.functions) == Counter(
            DEFINITION.findall(provider_text)), "owned parser omitted a helper definition"
        assert all(function.indexed_kernel is not None for function in provider_module.functions)
        # Direct real emitter call: BackendUnavailable or any other failure
        # propagates. There is no text-emitter, external-tool, or skip fallback.
        assembly = emit_aarch64_darwin_indexed_module(provider_module, optimize=False)
        for name in declared:
            label = "_user_" + PROVIDER.replace(".", "_") + "_" + name + ":"
            assert re.search(r"^" + re.escape(label) + r"\s*$", assembly, re.M), label
        (tmp_path / "indexed-handoff-closure.s").write_text(assembly, encoding="utf-8")
        receipt["assembly_sha256"] = _sha256(assembly.encode("utf-8"))
        receipt["provider_ir_sha256"] = _sha256(provider_text.encode("utf-8"))
        assert source_hashes == {name: _sha256(path.read_bytes()) for name, path in sources.items()}
        assert receipt["verified_functions"] == declared
        receipt.update(status="PASS", phase="complete")
    except Exception as error:
        receipt.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        (tmp_path / "indexed-handoff-closed-world.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
