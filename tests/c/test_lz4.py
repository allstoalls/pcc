import os

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import (
    collect_cpp_args,
    collect_translation_units,
    translation_unit_include_dirs,
)
from tests.parallel_jobs import translation_unit_jobs


PROJECT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PROJECTS_DIR = os.path.join(PROJECT_DIR, "projects")
LZ4_LIB_DIR = os.path.join(PROJECTS_DIR, "lz4-1.10.0", "lib")
LZ4_TEST_MAIN = os.path.join(PROJECTS_DIR, "test_lz4_main.c")
LZ4_MAKE_GOAL = "lib-release"


def _lz4_cpp_args():
    return tuple(collect_cpp_args(LZ4_LIB_DIR, sources_from_make=LZ4_MAKE_GOAL))


def _lz4_units():
    return collect_translation_units(
        LZ4_TEST_MAIN,
        dependencies=[f"{LZ4_LIB_DIR}={LZ4_MAKE_GOAL}"],
    )


@pytest.fixture(scope="module")
def lz4_compiled_units():
    units, base_dir = _lz4_units()
    compiled_units = CEvaluator().compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_lz4_cpp_args(),
    )
    return compiled_units, base_dir


@pytest.fixture(scope="module")
def lz4_compiled_units_self():
    units, base_dir = _lz4_units()
    compiled_units = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_lz4_cpp_args(),
    )
    return compiled_units, base_dir


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(LZ4_LIB_DIR) else "lz4-1.10.0/lib not found")
def test_lz4_make_goal_dependency_collects_library_sources():
    units, base_dir = _lz4_units()

    names = [unit.name for unit in units]

    assert base_dir == os.path.abspath(PROJECTS_DIR)
    assert names[-1] == "test_lz4_main.c"
    assert "lz4.c" in names
    assert "lz4frame.c" in names
    assert "lz4hc.c" in names
    assert "xxhash.c" in names
    assert "programs/lz4cli.c" not in names
    assert "examples/simple_buffer.c" not in names


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(LZ4_LIB_DIR) else "lz4-1.10.0/lib not found")
def test_lz4_runtime_with_mcjit_depends_on(lz4_compiled_units):
    compiled_units, _base_dir = lz4_compiled_units

    result = CEvaluator().evaluate_compiled_translation_units(
        compiled_units,
        optimize=True,
    )

    assert result == 0


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(LZ4_LIB_DIR) else "lz4-1.10.0/lib not found")
def test_lz4_runtime_with_system_link_depends_on(lz4_compiled_units):
    compiled_units, base_dir = lz4_compiled_units

    result = CEvaluator().run_compiled_translation_units_with_system_cc(
        compiled_units,
        optimize=True,
        base_dir=base_dir,
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"lz4 system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "lz4 version 1.10.0" in result.stdout
    assert "block roundtrip: hello, lz4!" in result.stdout
    assert "frame roundtrip: hello, lz4!" in result.stdout
    assert "OK" in result.stdout


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(LZ4_LIB_DIR) else "lz4-1.10.0/lib not found")
def test_lz4_runtime_with_self_backend_system_link_depends_on(lz4_compiled_units_self):
    compiled_units, base_dir = lz4_compiled_units_self

    result = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).run_compiled_translation_units_with_system_cc(
        compiled_units,
        optimize=True,
        base_dir=base_dir,
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"lz4 self backend system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "lz4 version 1.10.0" in result.stdout
    assert "block roundtrip: hello, lz4!" in result.stdout
    assert "frame roundtrip: hello, lz4!" in result.stdout
    assert "OK" in result.stdout
