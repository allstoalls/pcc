"""Native saved-error cleanup through the three affected public ABI routes.

The archive must be explicitly selected and match this runtime source tree.
The helper calls its real exported getters and metaclass entry, never copied
cleanup bodies. Ordinary __del__ saves/restores TLS itself, so these controls
also check error ownership and pin balance: those fail for the original byte
offset bug even when the finalizer dispatcher restores the pending error.
Unprotected TLS overwrite, arbitrary thread interleavings, and forced movement
remain separate model/native-gate obligations.
"""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("class_lookup_cleanup_probe.ll.in")

PROGRAM_PREFIX = '''import gc
from pcc.extern import c_int64, c_obj, extern

probe = extern(
    "test_class_lookup_cleanup",
    (c_obj, c_obj, c_int64, c_int64),
    c_int64,
)
original = None
calls = []
finalized = []
'''

PROGRAMS = {
    "metaclass": '''class TemporaryCall:
    def __call__(self):
        calls.append("metaclass")
        raise original
    def __del__(self):
        gc.collect()
        finalized.append("metaclass")

class CallDescriptor:
    def __get__(self, receiver, owner):
        return TemporaryCall()

class Meta(type):
    __call__ = CallDescriptor()

class Target(metaclass=Meta):
    pass

def make_receiver():
    return Target
''',
    "instance": '''class Descriptor:
    def __get__(self, receiver, owner):
        calls.append("instance")
        del owner.missing
        raise original
    def __set__(self, receiver, value):
        raise AssertionError("setup unexpectedly called the descriptor setter")
    def __del__(self):
        gc.collect()
        finalized.append("instance")

class Target:
    def __init__(self):
        self.missing = 0

def make_receiver():
    receiver = Target()
    Target.missing = Descriptor()
    return receiver
''',
    "custom-instance": '''class Target:
    def __init__(self):
        self.missing = 0
    def __getattribute__(self, name):
        calls.append("custom-instance")
        raise original

def make_receiver():
    return Target()
''',
}


def _program(entry):
    kind = {"metaclass": 0, "instance": 1, "custom-instance": 2}[entry]
    expected_finalizers = 0 if entry == "custom-instance" else 1
    suffix = f'''
def main():
    global original
    for prior_pin in (0, 64):
        original = ValueError("original lookup failure")
        calls.clear()
        finalized.clear()
        receiver = make_receiver()
        status = probe(receiver, original, {kind}, prior_pin)
        assert status == 0, ("cleanup probe status", status, prior_pin)
        assert calls == ["{entry}"], calls
        assert len(finalized) == {expected_finalizers}, finalized
        assert str(original) == "original lookup failure"
    print("CLASS_LOOKUP_CLEANUP_OK {entry}")
main()
'''
    return PROGRAM_PREFIX + PROGRAMS[entry] + suffix


def _probe_ir(target):
    from pcc.runtime.py import py_abi_constants as abi

    names = (
        "C_POINTER_SIZE",
        "PYINSTANCEOBJECT_CLS_OFFSET",
        "PYCLASSOBJECT_N_FIELDS_OFFSET",
        "PYCLASSOBJECT_FIELD_NAMES_OFFSET",
        "PYOBJECTHEADER_FLAGS_OFFSET",
        "PYOBJECTHEADER_REFCOUNT_OFFSET",
        "PY_FLAG_GC_PINNED",
    )
    values = {name: getattr(abi, name) for name in names}
    values.update(
        ROOT_SLOT_COUNT=3,
        ARGS_OFFSET=2 * abi.C_POINTER_SIZE,
        UNPIN_MASK=~abi.PY_FLAG_GC_PINNED,
    )
    text = PROBE.read_text()
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", str(value))
    # The comment's {{NAME}} is documentation, not an unresolved operand.
    assert "{{" not in "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(";")
    )
    return f'target triple = "{target}"\n' + text


@pytest.fixture(scope="module")
def explicit_class_cleanup_archive():
    from pcc.tools.runtime_archive_provenance import (
        PRODUCTION_POLICY,
        verify_runtime_archive_manifest,
    )

    selected = os.environ.get("PCC_RUNTIME_ARCHIVE")
    assert selected, "Set PCC_RUNTIME_ARCHIVE to a fresh matching full archive"
    archive = Path(selected).resolve(strict=True)
    assert archive.name == "libpy_runtime_pcc_py.a"
    manifest = verify_runtime_archive_manifest(
        archive,
        runtime_root=ROOT / "pcc/runtime",
        manifest_path=Path(str(archive) + ".provenance.json"),
    )
    assert manifest["policy"] == PRODUCTION_POLICY
    assert all(
        member["source_kind"] == "pcc-python"
        and member["producer_kind"] == "pcc-python-library-ir-to-obj"
        and member["uses_host_cc"] is False
        for member in manifest["members"]
    )
    return archive


@pytest.fixture(scope="module", params=tuple(PROGRAMS))
def class_cleanup_native_program(
    request, tmp_path_factory, explicit_class_cleanup_archive
):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text

    entry = request.param
    directory = tmp_path_factory.mktemp("class-cleanup-" + entry)
    target = host_target_triple()
    ir_text = _probe_ir(target)
    verify_ir_text(ir_text)
    (directory / "probe.ll").write_text(ir_text)
    helper = directory / "probe.o"
    helper.write_bytes(emit_owned_object(ir_text, target))
    source = directory / "class_cleanup.py"
    source.write_text(_program(entry))
    binary = directory / "class_cleanup"
    compile_python(
        str(source),
        str(binary),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(explicit_class_cleanup_archive),
        link_args=(str(helper),),
    )
    return entry, directory, binary


@pytest.mark.integration
@pytest.mark.parametrize("backend", range(5), ids=lambda value: f"gc{value}")
def test_native_class_lookup_cleanup_restores_error(
    class_cleanup_native_program, backend
):
    entry, directory, binary = class_cleanup_native_program
    result = subprocess.run(
        [str(binary)],
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(
            os.environ,
            PCC_GC_BACKEND=str(backend),
            PCC_GC_MINOR_ALLOC_MAX="4096",
            PCC_GC_REFCOUNT_PROVENANCE_PROBE="2",
            PCC_HOST_PYTHON="/nonexistent/host-python",
            PATH="",
        ),
    )
    (directory / f"gc{backend}.stdout").write_text(result.stdout)
    (directory / f"gc{backend}.stderr").write_text(result.stderr)
    assert result.returncode == 0, (
        entry, backend, result.returncode, result.stdout, result.stderr
    )
    assert result.stdout == f"CLASS_LOOKUP_CLEANUP_OK {entry}\n"
    assert result.stderr == "", (entry, backend, result.stderr)
