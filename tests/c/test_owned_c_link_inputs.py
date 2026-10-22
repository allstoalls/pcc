"""Caller-supplied relocatables reach the owned executable link boundary."""

import builtins
import subprocess

import pytest

from pcc.backend import BackendUnavailable, macho_spec
from pcc.backend.ar_writer import write_archive
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator, TranslationUnit


@pytest.fixture(autouse=True)
def deny_external_owners(monkeypatch):
    original = builtins.__import__

    def checked(name, *args, **kwargs):
        if name.split(".")[0] in ("llvmlite", "pycparser", "ply"):
            raise AssertionError("external compiler import: " + name)
        return original(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("external compiler process: " + repr(args))

    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _units(evaluator, source):
    return evaluator.compile_translation_units(
        [TranslationUnit(name="input.c", path="", source=source)],
        use_compile_cache=False,
    )


@pytest.mark.parametrize("archive", (False, True), ids=("object", "archive"))
def test_owned_darwin_executable_resolves_external_input(tmp_path, archive):
    evaluator = CEvaluator(target_triple="arm64-apple-darwin")
    helper = tmp_path / "helper.o"
    evaluator.emit_compiled_units(
        _units(evaluator, "int helper(int a) { return a + 35; }"),
        emit_obj=str(helper), optimize=False,
    )
    if archive:
        library = tmp_path / "libhelper.a"
        library.write_bytes(write_archive([("helper.o", helper.read_bytes())]))
        helper = library
    executable = tmp_path / "program"
    evaluator.emit_executable(
        _units(evaluator, "int helper(int); int main(void) { return helper(7); }"),
        str(executable), optimize=False, link_args=[str(helper)],
    )
    image = macho_spec.parse_object(executable.read_bytes())
    assert image.header["filetype"] == macho_spec.MH_EXECUTE
    symbols = {symbol["name"]: symbol for symbol in image.symbols()}
    assert symbols["_helper"]["n_type"] & macho_spec.N_TYPE == macho_spec.N_SECT
    assert symbols["_main"]["n_type"] & macho_spec.N_TYPE == macho_spec.N_SECT


@pytest.mark.parametrize("option", ("-unrecognized-pcc-link-option", "-Wl,-undefined,dynamic_lookup", "input.so"))
def test_owned_c_link_rejects_unsupported_options_before_emission(tmp_path, monkeypatch, option):
    evaluator = CEvaluator(target_triple="arm64-apple-darwin")

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported option reached emission")

    monkeypatch.setattr(evaluator, "_prepare_self_backend_units", forbidden)
    with pytest.raises(BackendUnavailable, match="unsupported.*link.*argument"):
        evaluator.emit_executable([], str(tmp_path / "output"), link_args=[option])


def test_owned_c_link_rejects_output_alias_before_overwriting(tmp_path):
    evaluator = CEvaluator(target_triple="arm64-apple-darwin")
    source = tmp_path / "helper.o"
    source.write_bytes(b"original")
    with pytest.raises(ValueError, match="output aliases"):
        evaluator.emit_executable([], str(source), link_args=[str(source)])
    assert source.read_bytes() == b"original"
