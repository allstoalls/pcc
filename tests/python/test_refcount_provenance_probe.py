"""PCC_GC_REFCOUNT_PROVENANCE_PROBE: the refcount hot path's managed-pointer
probe is a GC configuration, mirrored in py_obj.py and py_obj.c.

The probe (`pcc_gc_pointer_is_managed` before every header touch) was the
largest single share of self time on the virtual-thread gateway workload.
Its removal was DENIED on 2026-09-06 with a recorded prerequisite: the
PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS ratchet (116) had to read zero.  It
did on 2026-09-08, so the probe became a mode read once at
pcc_gc_config_ensure: 0 trusts the caller (default), 1 probes and counts,
2 probes, counts and reports the first miss on stderr.  These tests pin the
two mirrors to each other and the mode to its observable behaviour.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python.codegen import runtime_abi
from pcc.runtime.py import py_abi_constants as abi

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME = REPO_ROOT / "pcc" / "runtime"
PY_OBJ_PORT = (RUNTIME / "py" / "py_obj.py").read_text(encoding="utf-8")
GC_STATE_PORT = (RUNTIME / "py" / "freestanding_gc_state.py").read_text(
    encoding="utf-8"
)
GC_CONFIG_PORT = (
    RUNTIME / "py" / "freestanding_gc_public_collection.py"
).read_text(encoding="utf-8")
TELEMETRY_PORT = (RUNTIME / "py" / "py_gc_telemetry.py").read_text(
    encoding="utf-8"
)
HEADER = (RUNTIME / "include" / "py_runtime.h").read_text(encoding="utf-8")

ENV_NAME = "PCC_GC_REFCOUNT_PROVENANCE_PROBE"
MODE_METRIC = 117
UNMANAGED_METRIC = 116
FIRST_MISS_MESSAGE = (
    "pcc runtime: refcount operation on an unmanaged pointer "
    "(PCC_GC_REFCOUNT_PROVENANCE_PROBE=2)"
)


def test_both_mirrors_start_probing_until_config_is_read() -> None:
    # A refcount that runs before pcc_gc_config_ensure has read the env keeps
    # the historical check; only the configured value can turn it off.
    assert 'define_global_i32("pcc_gc_refcount_provenance_probe", 1)' in GC_STATE_PORT


def test_both_mirrors_parse_the_same_env_default_and_range() -> None:
    assert (
        'pcc_platform_getenv(cstr("PCC_GC_REFCOUNT_PROVENANCE_PROBE")), -1, -1, 3'
        in GC_CONFIG_PORT
    )
    # Unset resolves per backend: the relocating collectors keep the probe.
    assert "refcount_probe = 1 if (backend == 3 or backend == 4) else 0" in GC_CONFIG_PORT
    assert (
        'store_i32(global_addr("pcc_gc_refcount_provenance_probe"), 0, refcount_probe)'
        in GC_CONFIG_PORT
    )


def test_python_mirror_guards_every_refcount_probe_site() -> None:
    guarded = (
        "if _refcount_provenance_probe_enabled() != 0 and not _ptr_can_have_header(o):\n"
        "        _note_unmanaged_refcount_op()\n"
    )
    # incref/decref prepare (GC1-4) plus the GC0 inline paths of py_incref and
    # py_decref: four sites, every one behind the mode read.
    assert PY_OBJ_PORT.count(guarded) == 4
    unguarded = [
        line
        for line in PY_OBJ_PORT.splitlines()
        if line.strip() == "if not _ptr_can_have_header(o):"
    ]
    # The one remaining bare probe is _gc_relocation_candidate, a GC-internal
    # query rather than a refcount operation.
    assert len(unguarded) == 1
    candidate = PY_OBJ_PORT.split("def _gc_relocation_candidate(o) -> int:", 1)[1]
    assert candidate.lstrip().startswith("if not _ptr_can_have_header(o):")


def test_first_miss_report_is_identical_in_both_mirrors() -> None:
    assert FIRST_MISS_MESSAGE in PY_OBJ_PORT
    # Both mirrors gate the report on mode >= 2 and a once-only flag, and
    # abort in mode 3.
    assert "if mode >= 2:" in PY_OBJ_PORT
    assert "if mode == 3:\n                pcc_platform_abort()" in PY_OBJ_PORT
    assert 'global_addr("pcc_gc_refcount_provenance_probe_reported")' in PY_OBJ_PORT
    length = len((FIRST_MISS_MESSAGE + "\n").encode("utf-8"))
    assert f"                {length},\n" in PY_OBJ_PORT


def test_mode_is_readable_through_telemetry_in_both_mirrors() -> None:
    assert f"PCC_GC_COUNTER_REFCOUNT_PROVENANCE_PROBE = {MODE_METRIC}" in HEADER
    assert (
        f"if metric == {MODE_METRIC}:\n"
        '        return load_i32(global_addr("pcc_gc_refcount_provenance_probe"), 0)'
        in TELEMETRY_PORT
    )


def test_new_globals_are_registered_for_the_freestanding_closure() -> None:
    for name in (
        "pcc_gc_refcount_provenance_probe",
        "pcc_gc_refcount_provenance_probe_reported",
    ):
        assert name in runtime_abi.FREESTANDING_GC_I32_GLOBALS
        assert name in runtime_abi.FREESTANDING_GC_RUNTIME_GLOBALS


def _fake_object_program() -> str:
    # A malloc'd block that is not a managed object.  Its type tag is set to
    # -1 so that, when the probe is off, the ordinary header check rejects it
    # and neither refcount op touches the block's refcount word.
    return textwrap.dedent(
        f"""
        from pcc.extern import extern, c_int64, c_ptr, c_void
        from pcc.unsafe import free, malloc, memset, store_i32

        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        py_incref = extern("py_incref", (c_ptr,), c_void)
        py_decref = extern("py_decref", (c_ptr,), c_void)

        def main() -> None:
            raw = malloc(64)
            memset(raw, 0, 64)
            store_i32(raw, {abi.PYOBJECTHEADER_TYPE_TAG_OFFSET}, -1)
            print(pcc_gc_telemetry({MODE_METRIC}))
            before = pcc_gc_telemetry({UNMANAGED_METRIC})
            py_incref(raw)
            py_decref(raw)
            py_incref(raw)
            print(pcc_gc_telemetry({UNMANAGED_METRIC}) - before)
            free(raw)

        main()
        """
    )


@pytest.fixture(scope="module")
def fake_object_binary(tmp_path_factory) -> Path:
    from pcc.frontends.python.pipeline import compile_python

    tmp_path = tmp_path_factory.mktemp("refcount_probe")
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(_fake_object_program(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    return exe


def _run(
    exe: Path, mode: str | None, backend: str = "0"
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop(ENV_NAME, None)
    env["PCC_GC_BACKEND"] = backend
    if mode is not None:
        env[ENV_NAME] = mode
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60, env=env
    )


def test_default_mode_trusts_the_caller_and_counts_nothing(fake_object_binary) -> None:
    result = _run(fake_object_binary, None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "0"]
    assert FIRST_MISS_MESSAGE not in result.stderr


def test_mode_one_probes_and_counts_every_unmanaged_refcount(fake_object_binary) -> None:
    result = _run(fake_object_binary, "1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1", "3"]
    assert FIRST_MISS_MESSAGE not in result.stderr


def test_mode_two_reports_the_first_miss_once(fake_object_binary) -> None:
    result = _run(fake_object_binary, "2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["2", "3"]
    assert result.stderr.count(FIRST_MISS_MESSAGE) == 1


@pytest.mark.parametrize("backend", ["1", "2"])
def test_non_moving_collectors_default_to_trusting_the_caller(
    fake_object_binary, backend
) -> None:
    result = _run(fake_object_binary, None, backend)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "0"]


@pytest.mark.parametrize("backend", ["3", "4"])
def test_relocating_collectors_keep_the_probe_by_default(
    fake_object_binary, backend
) -> None:
    # A refcount can reach a pre-move address under relocation; the probe is
    # what turns that into a counted no-op instead of a header read.
    result = _run(fake_object_binary, None, backend)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1", "3"]
    # An explicit value still wins on every backend.
    result = _run(fake_object_binary, "0", backend)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "0"]


def test_mode_three_aborts_at_the_first_miss(fake_object_binary) -> None:
    result = _run(fake_object_binary, "3")
    # The mode is printed before the first refcount; the abort happens inside it.
    assert result.stdout.split() == ["3"], result.stdout
    assert result.returncode != 0
    assert result.stderr.count(FIRST_MISS_MESSAGE) == 1


def test_known_ref_checks_env_is_accepted(fake_object_binary) -> None:
    # The fake-object program uses the public py_incref path, so the switch
    # cannot change its counts; it must be accepted and leave behaviour intact.
    env = dict(os.environ)
    env.pop(ENV_NAME, None)
    env["PCC_GC_BACKEND"] = "0"
    env["PCC_GC_KNOWN_REF_CHECKS"] = "1"
    result = subprocess.run(
        [str(fake_object_binary)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "0"]


def test_out_of_range_values_clamp_like_every_other_gc_knob(fake_object_binary) -> None:
    result = _run(fake_object_binary, "7")
    # Clamped to 3: reported and aborted at the first miss.
    assert result.stdout.split()[0] == "3"
    assert result.returncode != 0
    result = _run(fake_object_binary, "-3")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "0"]
