"""Public Python object admission and owned-link handoff, without a runtime."""

import ast
import os
from pathlib import Path
import re
import struct

import pytest

from pcc.backend.target_objects import encode_assembly_object
from pcc.frontends.python import pipeline
from tests.python.test_windows_direct_scalar_call_args import (
    HOME_CALLBACK_ASM, MANAGED_CALLBACK_SOURCE, TARGET,
)


@pytest.fixture(autouse=True)
def _owned_mode(monkeypatch):
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    monkeypatch.setenv("PCC_NO_AUTO_PCC1", "1")
    monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", raising=False)
    monkeypatch.setattr(pipeline, "_python_frontend_jobs_for_sources", lambda _paths: 1)


def _compile(entry, source, output, **options):
    if entry == "single":
        return pipeline.compile_python(str(source), str(output), **options)
    return pipeline.compile_python_multi(
        [str(source)], str(output), module_names=["entry"], **options,
    )


def _object(target):
    if target == TARGET:
        return encode_assembly_object(HOME_CALLBACK_ASM, target)
    if target.startswith("x86_64"):
        assembly = ".intel_syntax noprefix\n.text\n.globl helper\nhelper:\n ret\n"
    else:
        assembly = ".section __TEXT,__text,regular,pure_instructions\n.globl helper\nhelper:\n ret\n"
    encoded = encode_assembly_object(assembly, target)
    if "darwin" in target:
        from pcc.backend.native_object import decode_native_object
        return decode_native_object(encoded).to_macho()
    return encoded


@pytest.mark.parametrize("entry", ("single", "multi"))
@pytest.mark.parametrize("target", (
    TARGET, "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "arm64-apple-darwin",
))
@pytest.mark.parametrize("suffix", (".o", ".obj"))
def test_public_object_admission_preserves_paths_and_bytes(tmp_path, monkeypatch, entry, target, suffix):
    source, output = tmp_path / "entry.py", tmp_path / "program"
    source.write_text("pass\n")
    output.write_bytes(b"distinct pre-existing output")
    helper = tmp_path / ("helper with spaces" + suffix)
    data = _object(target)
    helper.write_bytes(data)
    admitted = []

    class StopBeforeFrontend(Exception):
        pass

    def stop(*_args, **_kwargs):
        admitted.append(True)
        raise StopBeforeFrontend

    monkeypatch.setattr(pipeline, "_prepare_single_source_compile_closure", stop)
    monkeypatch.setattr(pipeline, "_prepare_multi_source_compile_closure", stop)
    with pytest.raises(StopBeforeFrontend):
        _compile(entry, source, output, backend="self", target_triple=target,
                 object_inputs=(str(helper),))
    assert admitted == [True]
    assert helper.read_bytes() == data
    assert output.read_bytes() == b"distinct pre-existing output"


@pytest.mark.parametrize("entry", ("single", "multi"))
def test_empty_object_inputs_do_not_require_file_identity(tmp_path, monkeypatch, entry):
    source, output = tmp_path / "entry.py", tmp_path / "program"
    source.write_text("pass\n")
    output.write_bytes(b"previous output")
    monkeypatch.delattr(os.path, "samefile")

    class FrontendReached(Exception):
        pass

    def stop(*_args, **_kwargs):
        raise FrontendReached

    monkeypatch.setattr(pipeline, "_prepare_single_source_compile_closure", stop)
    monkeypatch.setattr(pipeline, "_prepare_multi_source_compile_closure", stop)
    with pytest.raises(FrontendReached):
        _compile(entry, source, output, backend="self", target_triple=TARGET)
    assert output.read_bytes() == b"previous output"


@pytest.mark.parametrize("entry", ("single", "multi"))
@pytest.mark.parametrize("invalid", (
    "missing", "directory", "archive-suffix", "archive-content", "executable-suffix",
    "executable-content", "executable-flag", "dll-flag", "wrong-machine", "wrong-format", "truncated",
    "unsupported-target", "flag", "response-file", "bare-string", "output-alias",
    "temporary-alias", "hardlink-output", "hardlink-temporary", "case-alias",
    "unavailable-file-identity", "emit-only", "llvm", "cc",
))
def test_public_object_admission_fails_before_frontend_or_runtime(tmp_path, monkeypatch, entry, invalid):
    source, output, helper = tmp_path / "entry.py", tmp_path / "program.obj", tmp_path / "helper.obj"
    source.write_text("pass\n")
    data = _object(TARGET)
    helper.write_bytes(data)
    options = dict(backend="self", target_triple=TARGET)
    if invalid == "missing":
        helper = tmp_path / "absent.obj"
    elif invalid == "directory":
        helper = tmp_path / "directory.obj"
        helper.mkdir()
    elif invalid == "archive-suffix":
        helper = tmp_path / "helper.lib"
        helper.write_bytes(data)
    elif invalid == "archive-content":
        helper.write_bytes(b"!<arch>\n")
    elif invalid == "executable-suffix":
        helper = tmp_path / "helper.exe"
        helper.write_bytes(data)
    elif invalid == "executable-content":
        helper.write_bytes(b"MZ" + b"\0" * 254)
    elif invalid == "executable-flag":
        helper.write_bytes(data[:18] + struct.pack("<H", 0x0002) + data[20:])
    elif invalid == "dll-flag":
        helper.write_bytes(data[:18] + struct.pack("<H", 0x2000) + data[20:])
    elif invalid == "wrong-machine":
        helper.write_bytes(struct.pack("<H", 0xAA64) + data[2:])
    elif invalid == "wrong-format":
        helper.write_bytes(_object("x86_64-unknown-linux-gnu"))
    elif invalid == "truncated":
        helper.write_bytes(data[:19])
    elif invalid == "unsupported-target":
        options["target_triple"] = "aarch64-pc-windows-msvc"
    elif invalid == "flag":
        helper = "-Wl,helper.obj"
    elif invalid == "response-file":
        helper = "@helper.obj"
    elif invalid == "output-alias":
        output = helper
    elif invalid == "temporary-alias":
        output = tmp_path / "program"
        helper = tmp_path / "program.tmp"
        helper.write_bytes(data)
    elif invalid == "hardlink-output":
        os.link(helper, output)
    elif invalid == "hardlink-temporary":
        os.link(helper, str(output) + ".tmp")
    elif invalid == "case-alias":
        output = tmp_path / helper.name.upper()
        monkeypatch.setattr(os.path, "normcase", lambda path: path.casefold())
    elif invalid == "unavailable-file-identity":
        output.write_bytes(b"pre-existing output")
        monkeypatch.delattr(os.path, "samefile")
    elif invalid == "emit-only":
        options["emit_llvm_only"] = True
    elif invalid == "llvm":
        options["backend"] = "llvm"
    elif invalid == "cc":
        monkeypatch.setenv("PCC_SELF_LINK", "cc")
    options["object_inputs"] = str(helper) if invalid == "bare-string" else (str(helper),)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid object reached frontend/runtime/link work")

    for name in ("_prepare_single_source_compile_closure", "_prepare_multi_source_compile_closure",
                 "_ensure_runtime", "_explicit_runtime_archive", "_run_self_link_command"):
        monkeypatch.setattr(pipeline, name, forbidden)
    with pytest.raises(pipeline.PyPipelineError):
        _compile(entry, source, output, **options)
    assert source.read_text() == "pass\n"


@pytest.mark.parametrize("target,offset,replacement", (
    ("x86_64-unknown-linux-gnu", 18, struct.pack("<H", 183)),
    ("aarch64-unknown-linux-gnu", 18, struct.pack("<H", 62)),
    ("x86_64-unknown-linux-gnu", 16, struct.pack("<H", 2)),
    ("arm64-apple-darwin", 4, struct.pack("<I", 0x01000007)),
    ("arm64-apple-darwin", 12, struct.pack("<I", 2)),
))
def test_public_object_admission_checks_elf_and_macho_identity(tmp_path, target, offset, replacement):
    helper = tmp_path / "helper.o"
    data = _object(target)
    helper.write_bytes(data[:offset] + replacement + data[offset + len(replacement):])
    with pytest.raises(pipeline.PyPipelineError, match="invalid object input"):
        pipeline.compile_python("missing.py", str(tmp_path / "out"), backend="self",
                                target_triple=target, object_inputs=(str(helper),))


@pytest.mark.parametrize("entry", ("single", "multi"))
@pytest.mark.parametrize("skip_ll", ("0", "1"))
def test_public_callback_compile_reaches_owned_pe_link_with_object(tmp_path, monkeypatch, entry, skip_ll):
    """Actual frontend/emission/admission; only runtime validation and final PE publication are mocked."""
    from pcc.backend import owned_pe_link
    from pcc.backend.coff_x86_64 import parse_object
    from pcc.diagnostics.compile_observability import ObservabilityOptions, observed_compile

    source, output = tmp_path / "entry.py", tmp_path / "callback.exe"
    helper, runtime = tmp_path / "callback helper.obj", tmp_path / "admitted.a"
    source.write_text(MANAGED_CALLBACK_SOURCE)
    data = _object(TARGET)
    helper.write_bytes(data)
    runtime.write_bytes(b"mocked explicit runtime identity")
    monkeypatch.setenv("PCC_SELF_BACKEND_SKIP_LL_TEMP", skip_ll)
    calls, archives = [], []

    def admitted(path, **options):
        archives.append((path, options))
        assert path == str(runtime) and options["target_triple"] == TARGET
        return path

    def link(**options):
        calls.append(options)
        assert options["objects"][-1] == str(helper)
        assert options["objects"].count(str(helper)) == 1
        assert options["archives"] == [str(runtime)]
        assert not options["manifest"]
        if options["assembly"]:
            assembly = "\n".join(Path(path).read_text() for path in options["assembly"])
            assert "call pcc_win64_home_probe" in assembly
            assert "pcc_win64_managed_callback:" in assembly
        else:
            # Single-source closure may emit several owned COFF modules.
            internal = [parse_object(Path(path).read_bytes()) for path in options["objects"][:-1]]
            assert any(symbol.name == "pcc_win64_managed_callback" and symbol.section
                       for obj in internal for symbol in obj.symbols)
            assert any(obj.symbols[relocation.symbol].name == "pcc_win64_home_probe"
                       for obj in internal for section in obj.sections for relocation in section.relocations)
        obj = parse_object(Path(options["objects"][-1]).read_bytes())
        assert any(symbol.name == "pcc_win64_home_probe" and symbol.section for symbol in obj.symbols)
        # This sentinel cannot be mistaken for a native execution artifact.
        Path(options["output"]).write_bytes(b"MOCKED PE HANDOFF")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("host control attempted runtime provisioning or an external tool")

    monkeypatch.setattr(pipeline, "_explicit_runtime_archive", admitted)
    monkeypatch.setattr(pipeline, "_ensure_runtime", forbidden)
    monkeypatch.setattr(pipeline.subprocess, "run", forbidden)
    monkeypatch.setattr(owned_pe_link, "link_inputs", link)
    observed_compile(
        _compile, entry, source, output, options=ObservabilityOptions(),
        backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=TARGET,
        runtime_archive=str(runtime), object_inputs=(str(helper),),
    )
    assert len(calls) == len(archives) == 1
    assert output.read_bytes() == b"MOCKED PE HANDOFF"
    assert helper.read_bytes() == data


@pytest.mark.parametrize("argument", ("-Wl,--gc-sections", "helper.obj"))
def test_raw_link_args_are_not_reclassified_as_object_inputs(tmp_path, monkeypatch, argument):
    source, helper = tmp_path / "entry.py", tmp_path / "helper.obj"
    source.write_text("print(7)\n")
    helper.write_bytes(_object(TARGET))
    monkeypatch.setattr(pipeline, "_explicit_runtime_archive", lambda path, **_kwargs: path)
    with pytest.raises(pipeline.PyPipelineError, match="does not support link arguments"):
        pipeline.compile_python(
            str(source), str(tmp_path / "out.exe"), backend="self", libpython_mode="off",
            target_triple=TARGET, runtime_archive=str(tmp_path / "runtime.a"),
            object_inputs=(str(helper),), link_args=(argument,),
        )


def test_object_validator_has_strict_owned_codegen(tmp_path):
    """Exact added validator body, real provider export metadata; host IR only."""
    from pcc.frontends.python import pipeline_self_backend_link
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    owner = Path(pipeline_self_backend_link.__file__)
    source = owner.read_text()
    tree = ast.parse(source)
    definitions = [ast.get_source_segment(source, node) for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                   and node.name in ("validate_object_inputs", "SelfBackendLinkError")]
    assert len(definitions) == 2
    extracted = tmp_path / "validator.py"
    extracted.write_text("import os\n" + "\n\n".join(definitions) + "\n")
    root = Path(__file__).resolve().parents[2]
    leaves = ("self_backend_target_match", "coff_x86_64", "elf_x86_64", "macho_spec")
    names = ["validator", "os"] + ["pcc.backend." + leaf for leaf in leaves]
    paths = [str(extracted), str(root / "pcc/stdlib/os.py")] + [
        str(root / "pcc/backend" / (leaf + ".py")) for leaf in leaves
    ]
    modules, exports, _ = build_closed_world_context(paths, names)
    typed = infer_module(modules[0], external_exports=exports)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    codegen._skip_program_main = True
    ir = str(codegen.generate(typed))
    (tmp_path / "validator.ll").write_text(ir)
    verify_ir_text(ir)
    assert "strict.nolib.stub" not in ir
    assert not re.search(r"\bcall [^\n]*@py_cpy_", ir)
