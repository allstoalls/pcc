import os

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import (
    collect_cpp_args,
    collect_translation_units,
    translation_unit_include_dirs,
)


PROJECT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PROJECTS_DIR = os.path.join(PROJECT_DIR, "projects")
OPENSSL_DIR = os.path.join(PROJECTS_DIR, "openssl-3.4.1")
OPENSSL_TEST_MAIN = os.path.join(PROJECTS_DIR, "test_openssl_main.c")
OPENSSL_MAKE_GOAL = "libcrypto.a"
OPENSSL_SMOKE_SOURCES = (
    os.path.join(OPENSSL_DIR, "crypto", "mem_clr.c"),
    os.path.join(OPENSSL_DIR, "crypto", "sha", "sha256.c"),
)


def _openssl_cpp_args():
    return tuple(collect_cpp_args(OPENSSL_DIR, sources_from_make=OPENSSL_MAKE_GOAL))


def _openssl_make_goal_units():
    return collect_translation_units(
        OPENSSL_TEST_MAIN,
        dependencies=[f"{OPENSSL_DIR}={OPENSSL_MAKE_GOAL}"],
    )


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(OPENSSL_DIR) else "openssl-3.4.1 not found")
def test_openssl_make_goal_dependency_collects_library_sources():
    units, base_dir = _openssl_make_goal_units()

    names = [unit.name for unit in units]

    assert base_dir == os.path.abspath(PROJECTS_DIR)
    assert names[-1] == "test_openssl_main.c"
    assert "crypto/cversion.c" in names
    assert "crypto/sha/sha1_one.c" in names
    assert "providers/implementations/digests/sha2_prov.c" in names
    assert "apps/openssl.c" not in names
    assert "ssl/ssl_lib.c" not in names
    assert "test/asynciotest.c" not in names


def _openssl_smoke_units():
    return collect_translation_units(
        OPENSSL_TEST_MAIN,
        dependencies=list(OPENSSL_SMOKE_SOURCES),
        cpp_args=_openssl_smoke_cpp_args(),
    )


def _openssl_smoke_cpp_args():
    return (*_openssl_cpp_args(), "-USHA256_ASM")


@pytest.fixture(scope="module")
def openssl_compiled_units():
    units, base_dir = _openssl_smoke_units()
    compiled_units = CEvaluator().compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=1,
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_openssl_smoke_cpp_args(),
    )
    return compiled_units, base_dir


@pytest.fixture(scope="module")
def openssl_compiled_units_self():
    units, base_dir = _openssl_smoke_units()
    compiled_units = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=1,
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_openssl_smoke_cpp_args(),
    )
    return compiled_units, base_dir


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(OPENSSL_DIR) else "openssl-3.4.1 not found")
def test_openssl_runtime_with_mcjit_depends_on(openssl_compiled_units):
    compiled_units, _base_dir = openssl_compiled_units

    result = CEvaluator().evaluate_compiled_translation_units(
        compiled_units,
        optimize=True,
    )

    assert result == 0


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(OPENSSL_DIR) else "openssl-3.4.1 not found")
def test_openssl_runtime_with_system_link_depends_on(openssl_compiled_units):
    compiled_units, base_dir = openssl_compiled_units

    result = CEvaluator().run_compiled_translation_units_with_system_cc(
        compiled_units,
        optimize=True,
        base_dir=base_dir,
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"openssl system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "openssl version OpenSSL 3.4.1" in result.stdout
    assert "sha256: 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in result.stdout
    assert "OK" in result.stdout


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(OPENSSL_DIR) else "openssl-3.4.1 not found")
def test_openssl_runtime_with_self_backend_system_link_depends_on(openssl_compiled_units_self):
    compiled_units, base_dir = openssl_compiled_units_self

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
    ), f"openssl self backend system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "openssl version OpenSSL 3.4.1" in result.stdout
    assert "sha256: 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in result.stdout
    assert "OK" in result.stdout
