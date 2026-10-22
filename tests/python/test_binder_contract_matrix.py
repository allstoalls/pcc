"""One source corpus for CPython, owned IR, and supplied-runtime execution.

An owned-IR pass is not a semantic pass. In particular, a compile-time
L1CodegenError is a failure for every cell whose source catches TypeError.
There are no xfails, error-prefix normalizations, or automatic native builds
in the reference/IR tests. The integration controller requires an explicitly
selected, provenance-verified runtime and executes each cell under GC0--4.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile

import pytest


SURFACES = (
    "local_function", "imported_function", "local_method", "imported_method",
    "local_constructor", "imported_constructor",
)

LAUNCHER_MODULE = "binder_launcher"
LAUNCHER_SOURCE = "import binder_entry\n"

# Exact call expressions intentionally retain the spelling that selects each
# owner. Do not turn direct calls into aliases to make a failing cell compile.
CASES = {
    "required_mixed": ("required", "1, 2, c=3", "(1, 2, 3)", None, ["body"]),
    "all_kinds": ("all", "1, 2, 4, 5, c=3, extra=6", "(1, 2, (4, 5), 3, {'extra': 6})", None, ["body"]),
    "literal_defaults": ("all", "1", "(1, 2, (), 3, {})", None, ["body"]),
    "posonly_into_kwargs": ("all", "1, a=9", "(1, 2, (), 3, {'a': 9})", None, ["body"]),
    "kwonly_under_splats": ("required", "*(1, 2), **{'c': 3}", "(1, 2, 3)", None, ["body"]),
    "multiple_splats": ("all", "*(1,), *(2, 4, 5), c=3, extra=6", "(1, 2, (4, 5), 3, {'extra': 6})", None, ["body"]),
    "missing_positional": ("required", "1, c=3", None, "missing 1 required positional argument: 'b'", []),
    "missing_kwonly": ("required", "1, 2", None, "missing 1 required keyword-only argument: 'c'", []),
    "duplicate_binding": ("required", "1, 2, b=4, c=3", None, "got multiple values for argument 'b'", []),
    "unexpected_keyword": ("required", "1, 2, c=3, unknown=4", None, "got an unexpected keyword argument 'unknown'", []),
    "posonly_keyword_error": ("required", "a=1, b=2, c=3", None, "got some positional-only arguments passed as keyword arguments: 'a'", []),
    "surplus_positional": ("required", "1, 2, 3, c=4", None, "surplus", []),
    "reordered_evaluation": ("required", "mark('a', 1), c=mark('c', 3), b=mark('b', 2)", "(1, 2, 3)", None, ["a", "c", "b", "body"]),
    "star_evaluation_once": ("all", "*mark('star', (1, 2, 4)), c=mark('c', 3), extra=mark('extra', 6)", "(1, 2, (4,), 3, {'extra': 6})", None, ["star", "c", "extra", "body"]),
    "interleaved_mappings": ("all", "mark('a', 1), **mark('first', {'c': 3}), extra=mark('extra', 6), **mark('last', {'last': 7})", "(1, 2, (), 3, {'extra': 6, 'last': 7})", None, ["a", "first", "extra", "last", "body"]),
    "repeated_mapping_key": ("required", "1, 2, **mark('first', {'c': 3}), **mark('duplicate', {'c': 4}), later=mark('later', 5)", None, "mapping-duplicate", ["first", "duplicate"]),
    "explicit_run_before_failure": ("required", "1, 2, **mark('first', {'c': 3}), c=mark('duplicate', 4), later=mark('later', 5), **mark('unreached', {})", None, "mapping-duplicate", ["first", "duplicate", "later"]),
    "binding_failure_after_evaluation": ("required", "1, 2, b=mark('duplicate', 4), c=mark('c', 3), **mark('last', {})", None, "got multiple values for argument 'b'", ["duplicate", "c", "last"]),
    "nonstring_key_after_evaluation": ("all", "1, **mark('first', {1: 2}), later=mark('later', 5), **mark('last', {})", None, "nonstring", ["first", "later", "last"]),
    "nonmapping_stops_evaluation": ("all", "1, **mark('bad', 5), later=mark('unreached', 6)", None, "nonmapping", ["bad"]),
    "definition_defaults_once": ("defaults", "", None, None, ["default:pos", "default:kw", "body", "body"]),
    "sole_star_iteration_late": ("all", "*iteration_source(), c=mark('kw', 3)", "(1, 2, (4,), 3, {})", None, ["source", "kw", "iter:source", "body"]),
    "mixed_star_iteration_early": ("all", "1, *iteration_tail(), c=mark('kw', 3)", "(1, 2, (4,), 3, {})", None, ["tail", "iter:tail", "kw", "body"]),
    "multiple_star_iteration_early": ("all", "*iteration_head(), *iteration_tail(), c=mark('kw', 3)", "(1, 2, (4,), 3, {})", None, ["head", "iter:head", "tail", "iter:tail", "kw", "body"]),
    "sole_noniterable_after_keyword": ("all", "*mark('star', 5), c=mark('kw', 3)", None, "noniterable", ["star", "kw"]),
    "mixed_noniterable_before_keyword": ("all", "1, *mark('star', 5), c=mark('unreached', 3)", None, "noniterable", ["star"]),
    "duplicate_keywords_preempt_iteration": ("all", "*iteration_source(), **mark('first', {'c': 3}), c=mark('duplicate', 4)", None, "mapping-duplicate", ["source", "first", "duplicate"]),
}

CELL_IDS = tuple(surface + "--" + case for surface in SURFACES for case in CASES)
SIGNATURES = {
    "required": ("a, /, b, *, c", "(a, b, c)"),
    "all": ("a, /, b=2, *items, c=3, **extras", "(a, b, items, c, extras)"),
    "defaults": ("value=make_default('pos'), *, key=make_default('kw')", "(value, key)"),
}


def matrix_sources(cell_id):
    """Return ordered (module name, source) pairs and their exact expectation."""
    surface, case = cell_id.split("--")
    signature_kind, arguments, expected, error, events = CASES[case]
    operand_labels = re.findall(r"mark\('([^']+)', ", arguments)
    arguments = re.sub(r"mark\('([^']+)', ", lambda match: "step_" + match.group(1) + "(", arguments)
    signature, result = SIGNATURES[signature_kind]
    imported = surface.startswith("imported_")
    owner = "binder_provider" if imported else "binder_entry"
    is_method = surface.endswith("_method")
    is_constructor = surface.endswith("_constructor")
    definitions = "events = []\nseed = 'defining'\n"
    if signature_kind == "defaults":
        definitions += ("def make_default(label):\n"
                        "    events.append('default:' + label)\n"
                        "    return [seed, label]\n")
    if is_constructor:
        definitions += ("class Box:\n    def __init__(self, " + signature + "):\n"
                        "        events.append('body')\n        self.result = " + result + "\n")
        expression = ("provider." if imported else "") + "Box"
        binding_name, mapping_name = "Box.__init__", "Box"
    elif is_method:
        definitions += ("class Holder:\n    def target(self, " + signature + "):\n"
                        "        events.append('body')\n        return " + result + "\n")
        expression = "receiver.target"
        binding_name = mapping_name = "Holder.target"
    else:
        definitions += ("def target(" + signature + "):\n"
                        "    events.append('body')\n    return " + result + "\n")
        expression = ("provider." if imported else "") + "target"
        binding_name = mapping_name = "target"
    entry = ("import binder_provider as provider\nevents = provider.events\n" if imported else definitions)
    if is_method:
        entry += "receiver = " + ("provider." if imported else "") + "Holder()\n"
    for label in operand_labels:
        entry += ("def step_" + label + "(value):\n    events.append(" + repr(label) + ")\n"
                  "    return value\n")
    if "iteration_" in arguments:
        entry += ("class CallIterable:\n"
                  "    def __init__(self, label, values):\n"
                  "        self.label = label\n        self.values = values\n"
                  "    def __iter__(self):\n"
                  "        events.append('iter:' + self.label)\n"
                  "        return iter(self.values)\n")
        for label, values in (("source", "(1, 2, 4)"), ("head", "(1,)"), ("tail", "(2, 4)")):
            entry += ("def iteration_" + label + "():\n"
                      "    events.append(" + repr(label) + ")\n"
                      "    return CallIterable(" + repr(label) + ", " + values + ")\n")
    # Mutating the provider name after definition proves default capture belongs
    # to its defining module, rather than re-evaluation in the importing caller.
    if signature_kind == "defaults":
        entry += "seed = 'caller'\n" + ("provider.seed = 'changed'\n" if imported else "")
    entry += "def probe():\n"
    call = expression + "(" + arguments + ")"
    get_result = ".result" if is_constructor else ""
    if signature_kind == "defaults":
        entry += ("    first = " + call + get_result + "\n"
                  "    second = " + call + get_result + "\n"
                  "    assert first[0] == ['defining', 'pos']\n"
                  "    assert first[1] == ['defining', 'kw']\n"
                  "    assert first[0] is second[0]\n"
                  "    assert first[1] is second[1]\n"
                  "    first[0].append('mutated')\n"
                  "    assert second[0] == ['defining', 'pos', 'mutated']\n")
    elif error is not None:
        if error == "mapping-duplicate":
            message = owner + "." + mapping_name + "() got multiple values for keyword argument 'c'"
        elif error == "nonstring":
            message = "keywords must be strings"
        elif error == "nonmapping":
            # CPython 3.15's merged-keyword operand diagnostic is deliberately
            # unqualified, unlike its repeated-key diagnostic above.
            message = "Value after ** must be a mapping, not int"
        elif error == "noniterable":
            message = "Value after * must be an iterable, not int"
        elif error == "surplus":
            count = 3 if is_method or is_constructor else 2
            message = (binding_name + "() takes " + str(count) + " positional arguments but "
                       + str(count + 1) + " positional arguments (and 1 keyword-only argument) were given")
        else:
            message = binding_name + "() " + error
        entry += ("    try:\n        " + call + "\n"
                  "    except TypeError as error:\n        assert str(error) == " + repr(message) + ", str(error)\n"
                  "    else:\n        raise AssertionError('expected runtime TypeError')\n")
    else:
        entry += "    result = " + call + get_result + "\n    assert result == " + expected + "\n"
    entry += ("    assert events == " + repr(events) + ", events\n"
              "    print('BINDER_CONTRACT_OK')\nprobe()\n")
    sources = [("binder_provider", definitions)] if imported else []
    return sources + [("binder_entry", entry)]


def write_matrix_sources(directory: Path, cell_id):
    files, names = [], []
    # Keep the case in an imported library in both execution modes. Making
    # binder_entry the executable entry would change class __module__ to
    # __main__ and invalidate the otherwise identical qualified-error oracle.
    for name, source in matrix_sources(cell_id) + [(LAUNCHER_MODULE, LAUNCHER_SOURCE)]:
        path = directory / (name + ".py")
        path.write_text(source, encoding="utf-8")
        files.append(str(path))
        names.append(name)
    return files, names


@pytest.mark.parametrize("cell_id", CELL_IDS)
def test_binder_contract_reference(cell_id):
    # The matrix's published oracle is 3.15. Do not silently certify another
    # version's wording or keyword-run evaluation order as that oracle.
    assert sys.version_info[:2] == (3, 15), sys.version
    old = {name: sys.modules.get(name) for name in ("binder_provider", "binder_entry", LAUNCHER_MODULE)}
    old_path = sys.path[:]
    output = io.StringIO()
    try:
        with tempfile.TemporaryDirectory(prefix="pcc-binder-reference-") as directory:
            write_matrix_sources(Path(directory), cell_id)
            for name in old:
                sys.modules.pop(name, None)
            sys.path.insert(0, directory)
            with contextlib.redirect_stdout(output):
                runpy.run_path(str(Path(directory) / (LAUNCHER_MODULE + ".py")), run_name="__main__")
    finally:
        sys.path[:] = old_path
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    assert output.getvalue() == "BINDER_CONTRACT_OK\n"


def generate_matrix_ir(directory: Path, cell_id):
    """Reuse the closed-world owner/ABI path used by imported-constructor tests."""
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    files, names = write_matrix_sources(directory, cell_id)
    modules, exports, _ = build_closed_world_context(files, names)
    emitted = {}
    for module in modules:
        typed = infer_module(module, external_exports={key: value for key, value in exports.items() if key != module.name})
        codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        codegen._skip_program_main = module.name != LAUNCHER_MODULE
        text = str(codegen.generate(typed))
        (directory / (module.name + ".ll")).write_text(text, encoding="utf-8")
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
        emitted[module.name] = text
    return emitted


@pytest.mark.parametrize("cell_id", CELL_IDS)
def test_binder_contract_owned_ir(tmp_path, monkeypatch, cell_id):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    emitted = generate_matrix_ir(tmp_path, cell_id)
    text = emitted["binder_entry"]
    body = re.search(r"^define [^\n]*@user_binder_entry_probe\([^\n]*\).*?^}", text, re.M | re.S)
    assert body is not None
    # Every syntactic operand must be represented once and in source order.
    # Unique helper symbols make this observable without inferring from names
    # of tests or constants. Conditional
    # failure-edge execution order remains a native, rather than an IR, claim.
    source = matrix_sources(cell_id)[-1][1].split("def probe():\n", 1)[1]
    actual = re.findall(r"\bcall [^\n]*@user_binder_entry_step_([a-z]+)\(", body.group(0))
    expected = re.findall(r"step_([a-z]+)\(", source)
    assert actual == expected, (cell_id, actual, expected)
    iterator_calls = re.findall(r"\bcall [^\n]*@user_binder_entry_iteration_([a-z]+)\(", body.group(0))
    assert iterator_calls == re.findall(r"iteration_([a-z]+)\(", source)
    surface, case = cell_id.split("--")
    if case in ("sole_star_iteration_late", "mixed_star_iteration_early", "multiple_star_iteration_early"):
        kw = body.group(0).index("@user_binder_entry_step_kw(")
        expansion = body.group(0).index("@py_list_extend(")
        assert (kw < expansion) is (case == "sole_star_iteration_late")
    if case in ("required_mixed", "reordered_evaluation"):
        owner = "binder_provider" if surface.startswith("imported_") else "binder_entry"
        suffix = ("Box___init__" if surface.endswith("_constructor") else
                  "Holder_target" if surface.endswith("_method") else "target")
        target = "user_" + owner + "_" + suffix
        binder_calls = len(re.findall(r"\bcall [^\n]*@py_obj_call_slots\(", body.group(0)))
        direct_calls = len(re.findall(r"\bcall [^\n]*@" + target + r"\(", body.group(0)))
        # Keywords in formal order bind statically wherever the target's
        # signature is known; reordered keywords always use the binder.
        if case == "required_mixed" and surface != "imported_constructor":
            assert (binder_calls, direct_calls) == (0, 1), cell_id
        else:
            assert (binder_calls, direct_calls) == (1, 0), cell_id
            assert "@pcc_gc_root_copy_lease(" in body.group(0) or "@pcc_gc_foreign_lease_acquire(" in body.group(0)


@pytest.mark.integration
@pytest.mark.parametrize("cell_id", CELL_IDS)
def test_binder_contract_native_five_gc(tmp_path, monkeypatch, cell_id):
    """Central execution controller: supplied runtime only, never provisioning."""
    from pcc.frontends.python.pipeline import compile_python_multi
    import pcc
    from pcc.tools.runtime_archive_provenance import (
        PRODUCTION_POLICY, manifest_is_stale_for_current_codegen, verify_runtime_archive_manifest,
    )

    archive_text = os.environ.get("PCC_RUNTIME_ARCHIVE")
    assert archive_text, "select an explicitly verified, source-matched runtime archive"
    archive = Path(archive_text).resolve(strict=True)
    records = verify_runtime_archive_manifest(
        archive, runtime_root=Path(pcc.__file__).resolve().parent / "runtime",
        manifest_path=Path(str(archive) + ".provenance.json"),
    )
    assert records["policy"] == PRODUCTION_POLICY
    assert not manifest_is_stale_for_current_codegen(records), "runtime compiler identity does not match this source"
    assert all(member["source_kind"] == "pcc-python" and member["producer_kind"] == "pcc-python-library-ir-to-obj"
               and member["uses_host_cc"] is False for member in records["members"])
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    files, names = write_matrix_sources(tmp_path, cell_id)
    binary = tmp_path / "binder_contract"
    compile_python_multi(files, str(binary), module_names=names, entry_module=LAUNCHER_MODULE,
                         backend="self", libpython_mode="off", ir_scaffold_mode="on",
                         runtime_archive=str(archive))
    for backend in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], env=environment, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (cell_id, backend, result.stdout, result.stderr)
        assert result.stdout == "BINDER_CONTRACT_OK\n", (cell_id, backend, result.stdout)
        assert result.stderr == "", (cell_id, backend, result.stderr)
